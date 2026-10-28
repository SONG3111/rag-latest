"""REST and SSE endpoints for the news radar."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from ..assistant.service import (
    append_message,
    get_or_create_conversation,
    recent_messages,
)
from ..config import get_settings
from ..db import get_session
from ..llm.resilience import translate_provider_error
from ..models import (
    MessageRole,
    NewsDigest,
    NewsDigestStatus,
)
from .agent import NewsAgent, to_langchain_history
from .categories import DEFAULT_CATEGORY_IDS
from .schemas import (
    NewsCategoriesRead,
    NewsCategoryRead,
    NewsChatRequest,
    NewsDigestRead,
    NewsHealthRead,
    NewsMessageRead,
    NewsPreferenceRead,
    NewsPreferenceUpdate,
)
from .service import (
    create_manual_digest,
    default_preferences,
    digest_read,
    find_active_digest,
    fresh_digest_for_selection,
    generate_digest,
    latest_digest,
    list_categories,
    run_digest_job,
    save_preferences,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/news")

NEWS_CHANNEL = "news"
NEWS_CHAT_ID = "web-default"


@router.get("/categories", response_model=NewsCategoriesRead)
def categories() -> NewsCategoriesRead:
    settings = get_settings()
    return NewsCategoriesRead(
        categories=[NewsCategoryRead(**item) for item in list_categories()],
        defaults=list(DEFAULT_CATEGORY_IDS),
        custom_topic_limit=settings.news_custom_topic_limit,
    )


@router.get("/preferences", response_model=NewsPreferenceRead)
def preferences(session: Session = Depends(get_session)) -> NewsPreferenceRead:
    row = default_preferences(session)
    session.commit()
    return NewsPreferenceRead(
        categories=list(row.categories or []),
        custom_topics=list(row.custom_topics or []),
    )


@router.put("/preferences", response_model=NewsPreferenceRead)
def update_preferences(
    payload: NewsPreferenceUpdate,
    session: Session = Depends(get_session),
) -> NewsPreferenceRead:
    try:
        row = save_preferences(
            session,
            categories=payload.categories,
            custom_topics=payload.custom_topics,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    return NewsPreferenceRead(
        categories=list(row.categories or []),
        custom_topics=list(row.custom_topics or []),
    )


@router.get("/digests/latest", response_model=NewsDigestRead)
def latest(
    date: str | None = None,
    session: Session = Depends(get_session),
) -> NewsDigestRead:
    row = latest_digest(session, date=date)
    if row is None:
        raise HTTPException(status_code=404, detail="news digest not found")
    return digest_read(session, row)


@router.post(
    "/digests/refresh",
    response_model=NewsDigestRead,
    status_code=202,
)
def refresh(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> NewsDigestRead:
    preference = default_preferences(session)
    categories = list(preference.categories or DEFAULT_CATEGORY_IDS)
    topics = list(preference.custom_topics or [])
    active = find_active_digest(
        session, categories=categories, custom_topics=topics
    )
    if active is not None:
        session.commit()
        return digest_read(session, active)
    row = create_manual_digest(session)
    session.commit()
    if row.status is NewsDigestStatus.pending:
        background_tasks.add_task(run_digest_job, row.id)
    return digest_read(session, row)


@router.get("/digests/{digest_id}", response_model=NewsDigestRead)
def digest(digest_id: str, session: Session = Depends(get_session)) -> NewsDigestRead:
    row = session.get(NewsDigest, digest_id)
    if row is None:
        raise HTTPException(status_code=404, detail="news digest not found")
    return digest_read(session, row)


@router.post("/chat/stream")
async def chat_stream(
    payload: NewsChatRequest,
    session: Session = Depends(get_session),
) -> EventSourceResponse:
    settings = get_settings()
    conversation = get_or_create_conversation(
        session,
        channel=NEWS_CHANNEL,
        external_chat_id=NEWS_CHAT_ID,
        title="网页新闻雷达",
    )
    previous = recent_messages(
        session, conversation.id, settings.news_history_messages
    )
    user_row = append_message(
        session, conversation.id, MessageRole.user, payload.message
    )
    session.commit()
    history = to_langchain_history(previous)

    def digest_event(row: NewsDigest) -> dict[str, str]:
        return {
            "event": "digest",
            "data": json.dumps(
                {
                    "type": "digest",
                    "digest": digest_read(session, row).model_dump(mode="json"),
                },
                ensure_ascii=False,
                default=str,
            ),
        }

    async def events() -> AsyncIterator[dict]:
        streamed: list[str] = []
        collected: list[str] = []

        def save_partial(note: str = "") -> str:
            answer = (collected[-1] if collected else "".join(streamed)).strip()
            if note:
                answer = (answer + note).strip()
            append_message(
                session,
                conversation.id,
                MessageRole.assistant,
                answer,
            )
            session.commit()
            return answer

        try:
            async with asyncio.timeout(settings.chat_turn_timeout):
                preference = default_preferences(session)
                categories = list(preference.categories or DEFAULT_CATEGORY_IDS)
                topics = list(preference.custom_topics or [])
                digest_row = fresh_digest_for_selection(
                    session,
                    categories=categories,
                    custom_topics=topics,
                    settings=settings,
                )
                if digest_row is None:
                    active = find_active_digest(
                        session,
                        categories=categories,
                        custom_topics=topics,
                    )
                    created_here = active is None
                    digest_row = active or create_manual_digest(
                        session, settings=settings
                    )
                    session.commit()
                    yield digest_event(digest_row)
                    if created_here:
                        await generate_digest(
                            session, digest_row, settings=settings
                        )
                        session.commit()
                    else:
                        for _ in range(200):
                            session.expire_all()
                            digest_row = session.get(NewsDigest, digest_row.id)
                            if (
                                digest_row is None
                                or digest_row.status
                                in {
                                    NewsDigestStatus.ready,
                                    NewsDigestStatus.failed,
                                }
                            ):
                                break
                            session.commit()
                            await asyncio.sleep(1)
                    if digest_row is None:
                        raise RuntimeError("新闻快照生成过程中丢失")
                    yield digest_event(digest_row)
                    if digest_row.status is NewsDigestStatus.failed:
                        raise RuntimeError(
                            digest_row.error or "新闻快照生成失败"
                        )

                agent = NewsAgent(session, llm=None)
                async for event in agent.astream(payload.message, history):
                    if event.type == "done":
                        collected.append(str(event.data.get("content") or ""))
                        continue
                    if event.type == "token":
                        streamed.append(str(event.data.get("text") or ""))
                    yield {
                        "event": event.type,
                        "data": json.dumps(
                            event.to_payload(), ensure_ascii=False, default=str
                        ),
                    }
                answer = (collected[-1] if collected else "".join(streamed)).strip()
                assistant_row = append_message(
                    session,
                    conversation.id,
                    MessageRole.assistant,
                    answer,
                )
                session.commit()
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {
                            "type": "done",
                            "content": answer,
                            "message_id": assistant_row.id,
                            "conversation_id": conversation.id,
                        },
                        ensure_ascii=False,
                    ),
                }
        except asyncio.CancelledError:
            answer = save_partial("\n\n（本轮已中断，以上为已生成的部分。）")
            yield {
                "event": "done",
                "data": json.dumps(
                    {"type": "done", "content": answer},
                    ensure_ascii=False,
                ),
            }
        except TimeoutError:
            answer = save_partial("\n\n（本轮生成超时，以上为已生成的部分。）")
            yield {
                "event": "done",
                "data": json.dumps(
                    {"type": "done", "content": answer},
                    ensure_ascii=False,
                ),
            }
        except Exception as exc:
            logger.exception("news chat failed")
            save_partial("\n\n（本轮处理失败，请稍后重试。）")
            yield {
                "event": "error",
                "data": json.dumps(
                    {"type": "error", "message": translate_provider_error(exc)},
                    ensure_ascii=False,
                ),
            }

    return EventSourceResponse(events())


@router.get("/messages", response_model=list[NewsMessageRead])
def messages(session: Session = Depends(get_session)) -> list[NewsMessageRead]:
    row = get_or_create_conversation(
        session,
        channel=NEWS_CHANNEL,
        external_chat_id=NEWS_CHAT_ID,
        title="网页新闻雷达",
    )
    session.commit()
    return [
        NewsMessageRead.model_validate(item, from_attributes=True)
        for item in recent_messages(session, row.id, 200)
    ]


@router.get("/health", response_model=NewsHealthRead)
def health(session: Session = Depends(get_session)) -> NewsHealthRead:
    settings = get_settings()
    last = session.scalar(
        select(NewsDigest).order_by(NewsDigest.created_at.desc()).limit(1)
    )
    last_stats = (last.source_stats or {}).get("providers", {}) if last else {}

    def provider(name: str, *, enabled: bool, **extra: object) -> dict:
        return {
            "enabled": enabled,
            "last_run": last_stats.get(name, {}),
            **extra,
        }

    return NewsHealthRead(
        enabled=settings.news_enabled,
        timezone=settings.news_timezone,
        generation_mode="on_demand",
        cache_minutes=settings.news_cache_minutes,
        providers={
            "domestic_rss": provider("domestic_rss", enabled=True),
            "domestic_hot": provider(
                "domestic_hot",
                enabled=bool(settings.news_hot_api_base_url.strip()),
                base_url=settings.news_hot_api_base_url,
            ),
            "google_news": provider(
                "google_news", enabled=settings.news_enable_google
            ),
            "global_news": provider(
                "global_news", enabled=settings.news_enable_global
            ),
            "extra_rss": provider(
                "extra_rss", enabled=bool(settings.news_extra_rss_urls)
            ),
        },
        last_digest_status=last.status.value if last else None,
        last_digest_at=last.completed_at or last.created_at if last else None,
        last_error=last.error if last else None,
    )
