"""Assembly of the persisted answer text for one chat turn.

Moved verbatim from the retired ``api/routes.py`` during the Java-backend
migration (M4): the stateless chat endpoint assembles the same answer for its
persist frame, and backend-java mirrors this logic in ``Answers.java`` — the
two implementations must stay in sync.
"""

from __future__ import annotations

from ..agent.prompts import PROPOSAL_NOTICE

EMPTY_ANSWER_FALLBACK = "模型这次没有返回内容，请再说一次。"


def compose_answer(collected: list[str], proposal_count: int) -> str:
    """Assemble the text persisted for an assistant turn.

    A provider occasionally ends a turn with an empty completion after a tool call.
    Persisting that verbatim leaves an empty bubble in the UI with no way to tell it
    apart from a broken app, so an empty turn gets an explicit placeholder.
    """
    answer = "\n\n".join(text for text in collected if text.strip())
    if proposal_count:
        answer = (answer + "\n\n" if answer else "") + PROPOSAL_NOTICE.format(
            count=proposal_count
        )
    return answer or EMPTY_ANSWER_FALLBACK
