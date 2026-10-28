"""Built-in industry taxonomy and query routing for the news radar.

The profile-style taxonomy and per-category quota mirror the approach used by
Thysrael/Horizon (MIT): a fixed editorial profile controls what belongs in each
section, while the runtime chooses which profiles are active.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CategorySpec:
    id: str
    label: str
    description: str
    queries_zh: tuple[str, ...]
    queries_en: tuple[str, ...]
    direct_feeds: tuple[tuple[str, str], ...] = ()


# User-facing order is intentional: the first three are the first-run defaults.
CATEGORY_SPECS: tuple[CategorySpec, ...] = (
    CategorySpec(
        "technology",
        "科技",
        "人工智能、芯片、软件、互联网与前沿技术",
        ("人工智能 重大进展", "半导体 芯片 科技公司", "网络安全 科技政策"),
        ("artificial intelligence major announcement", "semiconductor technology"),
        (),
    ),
    CategorySpec(
        "finance",
        "财经",
        "市场、央行、公司财报与宏观经济",
        ("全球市场 央行 重大", "公司 财报 并购", "经济政策 金融风险"),
        ("global markets central bank", "major earnings acquisition"),
        (("https://www.chinanews.com.cn/rss/finance.xml", "中国新闻网 财经"),),
    ),
    CategorySpec(
        "world",
        "政经国际",
        "地缘政治、选举、冲突与国际政策",
        ("国际 重大新闻", "地缘政治 外交", "选举 冲突 国际政策"),
        ("world major news", "geopolitics diplomacy election"),
        (
            ("https://www.chinanews.com.cn/rss/world.xml", "中国新闻网 国际"),
        ),
    ),
    CategorySpec(
        "society",
        "社会",
        "公共安全、人口、劳动与社会政策",
        ("社会 公共政策 重大", "人口 劳动 公共安全"),
        ("society public policy", "population labor public safety"),
        (("https://www.chinanews.com.cn/rss/china.xml", "中国新闻网 国内"),),
    ),
    CategorySpec(
        "health",
        "健康",
        "公共卫生、医学研究、药品与医疗改革",
        ("公共卫生 医学研究", "药品 医疗政策 重大"),
        ("public health medical research", "drug health policy"),
        (),
    ),
    CategorySpec(
        "education",
        "教育",
        "学校、招生、科研与教育政策",
        ("教育改革 重要政策", "大学 招生 科研"),
        ("education policy", "university research major"),
        (),
    ),
    CategorySpec(
        "culture_sports",
        "文体娱乐",
        "体育赛事、影视、音乐与文化事件",
        ("体育 重大赛事", "影视 娱乐 文化 重要"),
        ("major sports championship", "film entertainment culture"),
        (
            ("https://www.chinanews.com.cn/rss/sports.xml", "中国新闻网 体育"),
            ("https://www.chinanews.com.cn/rss/culture.xml", "中国新闻网 文化"),
        ),
    ),
    CategorySpec(
        "auto",
        "汽车",
        "车企、车型、智能驾驶、召回与产业政策",
        ("汽车 重大新闻", "智能驾驶 新能源车 召回"),
        ("automotive major news", "electric vehicle recall"),
        (),
    ),
    CategorySpec(
        "energy",
        "能源",
        "油气、电力、新能源、矿产与能源安全",
        ("能源 重大新闻", "石油 天然气 电力 新能源"),
        ("energy major news", "oil gas power grid"),
        (),
    ),
    CategorySpec(
        "consumer",
        "消费",
        "零售、电商、消费品牌与服务趋势",
        ("消费 零售 重大", "电商 消费品牌"),
        ("consumer retail major", "ecommerce consumer brand"),
        (),
    ),
)

CATEGORY_BY_ID = {item.id: item for item in CATEGORY_SPECS}
CATEGORY_ID_BY_LABEL = {item.label: item.id for item in CATEGORY_SPECS}
DEFAULT_CATEGORY_IDS = ["technology", "finance", "world"]
CUSTOM_PREFIX = "custom:"

# Used only to route a shared domestic feed into the user's selected categories.
# The relevance model still performs the final editorial decision.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "technology": (
        "人工智能", "AI", "芯片", "半导体", "大模型", "机器人", "软件",
        "互联网", "手机", "网络安全", "操作系统", "算力",
    ),
    "finance": (
        "央行", "金融", "银行", "股市", "基金", "债券", "汇率", "财报",
        "并购", "经济", "GDP", "利率", "通胀", "货币",
    ),
    "world": (
        "国际", "外交", "地缘", "美国", "欧洲", "俄罗斯", "乌克兰",
        "中东", "日本", "韩国", "全球", "联合国", "冲突",
    ),
    "society": (
        "社会", "公安", "法院", "人口", "就业", "劳动", "公共安全",
        "事故", "民生", "政策", "民政", "法治",
    ),
    "health": (
        "医疗", "医院", "药品", "疾病", "健康", "卫生", "疫苗", "医保",
        "医生", "患者",
    ),
    "education": (
        "教育", "高校", "大学", "招生", "考试", "教师", "学生", "科研",
        "学位", "学校",
    ),
    "culture_sports": (
        "体育", "足球", "篮球", "奥运", "电影", "影视", "音乐", "文化",
        "演出", "明星", "票房", "赛事",
    ),
    "auto": (
        "汽车", "车企", "新能源车", "电动车", "智驾", "自动驾驶", "车型",
        "召回", "车市",
    ),
    "energy": (
        "能源", "石油", "天然气", "电力", "电网", "光伏", "风电", "煤炭",
        "储能", "油价",
    ),
    "consumer": (
        "消费", "零售", "电商", "品牌", "餐饮", "旅游", "家电", "购物",
        "平台经济",
    ),
}


def category_label(category_id: str) -> str:
    if category_id.startswith(CUSTOM_PREFIX):
        return category_id[len(CUSTOM_PREFIX) :]
    spec = CATEGORY_BY_ID.get(category_id)
    return spec.label if spec else category_id


def category_id(value: str) -> str:
    """Accept either a stable category id or the label shown to the model."""

    cleaned = str(value or "").strip()
    if cleaned.startswith(CUSTOM_PREFIX) or cleaned in CATEGORY_BY_ID:
        return cleaned
    return CATEGORY_ID_BY_LABEL.get(cleaned, cleaned)


def custom_topic(category_id: str) -> str | None:
    if category_id.startswith(CUSTOM_PREFIX):
        return category_id[len(CUSTOM_PREFIX) :]
    return None


def normalize_custom_topics(topics: list[str], *, limit: int = 3) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in topics:
        value = " ".join(str(raw).split()).strip()
        if not value:
            continue
        if len(value) < 2 or len(value) > 40:
            raise ValueError("自定义主题长度必须在 2 到 40 个字符之间")
        if any(ord(char) < 32 for char in value):
            raise ValueError("自定义主题不能包含控制字符")
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
    if len(cleaned) > max(1, limit):
        raise ValueError(f"最多只能添加 {limit} 个自定义主题")
    return cleaned


def validate_category_selection(
    categories: list[str],
    custom_topics: list[str],
    *,
    custom_limit: int = 3,
) -> tuple[list[str], list[str]]:
    requested = list(dict.fromkeys(str(item).strip() for item in categories if str(item).strip()))
    unknown = [item for item in requested if item not in CATEGORY_BY_ID]
    if unknown:
        raise ValueError(f"未知新闻分类：{', '.join(unknown)}")
    topics = normalize_custom_topics(custom_topics, limit=custom_limit)
    if not requested and not topics:
        raise ValueError("至少选择一个新闻分类或自定义主题")
    return requested, topics


def active_category_ids(
    categories: list[str], custom_topics: list[str]
) -> list[str]:
    return [
        *categories,
        *(f"{CUSTOM_PREFIX}{topic}" for topic in custom_topics),
    ]


def category_queries(category_id: str) -> tuple[tuple[str, str], ...]:
    """Return ``(query, language)`` pairs for a built-in or custom category."""

    topic = custom_topic(category_id)
    if topic is not None:
        return ((topic, "zh"),)
    spec = CATEGORY_BY_ID[category_id]
    return tuple(
        [*( (query, "zh") for query in spec.queries_zh), *( (query, "en") for query in spec.queries_en)]
    )


def direct_feeds(category_id: str) -> tuple[tuple[str, str], ...]:
    spec = CATEGORY_BY_ID.get(category_id)
    return spec.direct_feeds if spec else ()

