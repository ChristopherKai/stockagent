"""网页搜索与网页 / PDF 全文抓取，供研究 agent 核实信息。

搜索源分两类：
- 搜索 API：Exa（.env 有 EXA_API_KEY 时直接调用接口；否则经 agent-reach 配置的 mcporter 调用，免费额度有限），
  以及博查、Tavily、Serper（.env 里填了 key 才启用）。稳定、结果带正文摘要，排在最前面；
- 免费来源（360、头条、Google 新闻、百度、Bing、DuckDuckGo）：同一 IP 短时间请求太多会被拦截——360 和百度出验证码，
  头条返回空结果页，DuckDuckGo 返回验证页（HTTP 202），Bing 把程序识别出来后返回毫不相关的热门网页。所以：
  配置中的前 ROTATE 个免费源轮流使用，同一来源两次请求之间有最小间隔；结果都要过相关性检查，无关就换下一个来源；
  被拦截的来源暂停一段时间；相同的检索式直接用缓存结果。
全部失败时告诉模型「搜索服务不可用」，而不是让它以为信息不存在。
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
import time
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from ..utils import CST, clean_text, to_simplified

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)  # 部分网页实为 XML，按 HTML 取文字即可

# 搜索与抓取用独立的会话：不带自动重试、超时更短，失败就快速换下一个搜索源
_http = requests.Session()
_http.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
})
SEARCH_TIMEOUT = (5, 12)
FETCH_TIMEOUT = (8, 25)
BLOCK_SECONDS = 1800  # 被拦截的搜索源暂停多久（验证码通常要持续一段时间，期间再请求只会延长封禁）
MIN_INTERVAL = 4.0    # 同一个免费搜索源两次请求的最小间隔（研究子任务并行时避免触发反爬）
ROTATE = 3            # 配置中前几个免费搜索源轮流使用（分摊请求量），其余只在它们都失败时备用

LABELS = {"exa": "Exa", "bocha": "博查", "tavily": "Tavily", "serper": "Serper（Google）", "so360": "360搜索", "toutiao": "头条搜索",
          "gnews": "Google 新闻", "baidu": "百度", "bing": "Bing", "duckduckgo": "DuckDuckGo"}
KEYS = {"bocha": "BOCHA_API_KEY", "tavily": "TAVILY_API_KEY", "serper": "SERPER_API_KEY"}  # 付费 API 读取 key 的环境变量
MCPORTER = shutil.which("mcporter")  # agent-reach 用它调用 Exa 的 MCP 服务
EXA_TIMEOUT = 60
# 靠脚本渲染、直接抓取拿不到正文的网站：直接用 Exa 读取
RENDERED_SITES = ("futunn.com", "moomoo.com", "xueqiu.com")


def api_providers() -> set[str]:
    """当前可用的搜索 API（不需要轮流、不需要限速）。Exa：.env 里有 EXA_API_KEY 时直接调用接口，否则经 mcporter 调用。"""
    out = {p for p, env in KEYS.items() if os.environ.get(env)}
    if os.environ.get("EXA_API_KEY") or MCPORTER:
        out.add("exa")
    return out


EXA_LIMIT_HINT = ("Exa 免费额度已用完（HTTP 429）。到 https://dashboard.exa.ai/api-keys 创建 key，"
                  "填进 .env 的 EXA_API_KEY，或按 agent-reach 的说明给 mcporter 的 exa 配置 key")


def _exa_rest(path: str, body: dict) -> dict:
    try:
        return _post_json(f"https://api.exa.ai/{path}", body, {"x-api-key": os.environ["EXA_API_KEY"]})
    except Blocked as e:
        raise Blocked(f"Exa 接口：{e}", e.seconds) from e

_blocked: dict[str, float] = {}
_last: dict[str, float] = {}
_cache: dict[tuple, tuple[list[dict], str]] = {}
_lock = threading.Lock()


class Blocked(Exception):
    """搜索源返回了验证页、拦截页或拒绝了 key；seconds 是暂停多久。"""

    def __init__(self, msg: str, seconds: float = BLOCK_SECONDS):
        super().__init__(msg)
        self.seconds = seconds


def _pace(name: str) -> None:
    if name in KEYS or name == "exa":
        return
    with _lock:
        now = time.time()
        at = max(now, _last.get(name, 0) + MIN_INTERVAL)
        _last[name] = at
    if at > now:
        time.sleep(at - now)


def _post_json(url: str, body: dict, headers: dict) -> dict:
    r = requests.post(url, json=body, headers=headers, timeout=SEARCH_TIMEOUT)
    if r.status_code in (401, 402, 403):
        raise Blocked(f"key 无效或额度不足（HTTP {r.status_code}）", 6 * 3600)
    if r.status_code == 429:
        raise Blocked("请求太频繁（HTTP 429）", 120)
    r.raise_for_status()
    return r.json()


def _bocha(query: str, n: int) -> list[dict]:
    d = _post_json("https://api.bochaai.com/v1/web-search", {"query": query, "summary": True, "count": n, "freshness": "noLimit"},
                   {"Authorization": f"Bearer {os.environ[KEYS['bocha']]}"})
    pages = ((d.get("data") or {}).get("webPages") or {}).get("value") or []
    return [_item(x.get("name", ""), x.get("url", ""),
                  " ".join(filter(None, [x.get("datePublished", "")[:10], x.get("siteName"), x.get("summary") or x.get("snippet")])))
            for x in pages[:n]]


def _tavily(query: str, n: int) -> list[dict]:
    d = _post_json("https://api.tavily.com/search", {"query": query, "max_results": n, "search_depth": "basic"},
                   {"Authorization": f"Bearer {os.environ[KEYS['tavily']]}"})
    return [_item(x.get("title", ""), x.get("url", ""), " ".join(filter(None, [x.get("published_date", "")[:10], x.get("content")])))
            for x in (d.get("results") or [])[:n]]


def _serper(query: str, n: int) -> list[dict]:
    cjk = bool(re.search(r"[\u4e00-\u9fff]", query))
    d = _post_json("https://google.serper.dev/search", {"q": query, "num": n, **({"hl": "zh-cn", "gl": "cn"} if cjk else {})},
                   {"X-API-KEY": os.environ[KEYS["serper"]]})
    return [_item(x.get("title", ""), x.get("link", ""), " ".join(filter(None, [x.get("date"), x.get("snippet")])))
            for x in (d.get("organic") or [])[:n]]


def _exa_call(tool: str, args: dict) -> str:
    """经 mcporter 调用 Exa（agent-reach 的配置）。程序和参数都是固定的列表，不经过 shell，参数只当数据传递。"""
    if not MCPORTER:
        raise Blocked("本机没有 mcporter", 24 * 3600)
    try:
        p = subprocess.run([MCPORTER, "call", f"exa.{tool}", "--output", "json", "--args", json.dumps(args, ensure_ascii=False)],
                           capture_output=True, text=True, timeout=EXA_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise requests.Timeout("Exa 超时") from e
    if p.returncode != 0:
        out = f"{p.stdout}\n{p.stderr}"
        if "429" in out or "rate limit" in out.lower():
            raise Blocked(EXA_LIMIT_HINT, 3600)
        raise Blocked(f"mcporter 返回 {p.returncode}：{_flat(p.stderr or p.stdout)[:100]}", 300)
    d = json.loads(p.stdout or "{}")
    if isinstance(d, dict) and d.get("isError"):
        raise Blocked(f"Exa 报错：{_flat(json.dumps(d, ensure_ascii=False))[:100]}", 300)
    content = d.get("content") if isinstance(d, dict) else None
    return "\n".join(c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text")


def parse_exa_results(text: str, n: int) -> list[dict]:
    """Exa 的结果是「Title / URL / Published / Author / Highlights」文本块，用 --- 分隔。"""
    out = []
    for block in re.split(r"\n-{3,}\n", text):
        url = re.search(r"^URL:\s*(\S+)", block, re.M)
        if not url:
            continue
        title = re.search(r"^Title:\s*(.+)$", block, re.M)
        pub = re.search(r"^Published:\s*(\d{4}-\d{2}-\d{2})", block, re.M)
        body = block.split("Highlights:", 1)[1] if "Highlights:" in block else ""
        out.append(_item(title.group(1) if title else url.group(1), url.group(1),
                         f"{pub.group(1) if pub else ''} {body}", limit=1200))
        if len(out) >= n:
            break
    return out


def _exa(query: str, n: int) -> list[dict]:
    if os.environ.get("EXA_API_KEY"):
        d = _exa_rest("search", {"query": query, "numResults": n, "type": "auto",
                                 "contents": {"highlights": {"numSentences": 3, "highlightsPerUrl": 3}}})
        return [_item(x.get("title") or x.get("url", ""), x.get("url", ""),
                      f"{(x.get('publishedDate') or '')[:10]} {' '.join(x.get('highlights') or []) or x.get('text') or ''}", limit=1200)
                for x in (d.get("results") or [])[:n]]
    text = _exa_call("web_search_exa", {
        "query": query.lstrip("@"), "numResults": n,
        "objective": f"找出与「{query}」直接相关的原始报道、公告、研报摘要或数据页面，优先最近发布和权威来源，摘出其中的具体数字和日期"})
    return parse_exa_results(text, n)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _item(title: str, url: str, snippet: str, limit: int = 400) -> dict:
    title, snippet = _flat(title), _flat(snippet)
    if snippet.startswith(title):
        snippet = snippet[len(title):].strip()
    return {"title": title, "url": url, "snippet": snippet[:limit]}


def _so360(query: str, n: int) -> list[dict]:
    r = _http.get("https://www.so.com/s", params={"q": query}, timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    if "qcaptcha" in r.url:
        raise Blocked("360 验证码")
    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.select("li.res-list")
    if not items and "360搜索" not in (soup.title.get_text() if soup.title else ""):
        raise Blocked("返回的不是搜索结果页")
    out = []
    for li in items:
        a = li.select_one("h3 a")
        if a is None:
            continue
        url = a.get("data-mdurl") or a.get("href") or ""  # data-mdurl 是真实地址，href 是 360 的跳转链接
        if (urlparse(url).hostname or "").endswith("ai.so.com"):  # AI 生成的问答页不是原始出处
            continue
        sn = li.select_one(".res-desc") or li.select_one(".res-rich")
        out.append(_item(a.get_text(), url, (sn or li).get_text()))
        if len(out) >= n:
            break
    return out


def _toutiao_url(href: str) -> str:
    """头条的跳转链接可能套了两层：sou.toutiao.com/search/jump?url=<再一层跳转或真实地址>。"""
    for _ in range(3):
        if "toutiao.com/search/jump" not in href:
            break
        href = parse_qs(urlparse(href).query).get("url", [href])[0]
    return href


def _toutiao(query: str, n: int) -> list[dict]:
    r = _http.get("https://so.toutiao.com/search", params={"keyword": query, "pd": "information", "dvpf": "pc"},
                  timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    cards = soup.select("div.result-content")
    if not cards and "头条搜索" not in (soup.title.get_text() if soup.title else ""):
        raise Blocked("返回的不是搜索结果页")
    out = []
    for c in cards:
        a = c.select_one(".cs-header a")
        if a is None or not a.get("href"):
            continue
        body = c.select_one(".cs-header ~ div .cs-text") or c
        src = c.select_one(".cs-source")
        snippet = body.get_text() + (f"（{_flat(src.get_text(' '))}）" if src else "")
        out.append(_item(a.get_text(), _toutiao_url(a["href"]), snippet))
        if len(out) >= n:
            break
    if not out and re.search(r"[\u4e00-\u9fff]", query):  # 头条对中文检索几乎总有结果；空页通常是被限流
        raise Blocked("返回空结果页，可能被限流", 300)
    return out


def _gnews(query: str, n: int) -> list[dict]:
    """Google 新闻 RSS：只有标题、媒体和日期，适合查目标价、价格、政策等新闻线索；链接由 fetch_text 解析成原文地址。"""
    hl, gl, ceid = ("zh-CN", "CN", "CN:zh-Hans") if re.search(r"[\u4e00-\u9fff]", query) else ("en-US", "US", "US:en")
    r = _http.get("https://news.google.com/rss/search", params={"q": query, "hl": hl, "gl": gl, "ceid": ceid}, timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    out = []
    for it in ET.fromstring(r.content).findall("./channel/item")[:n]:
        title, media = _flat(it.findtext("title", "")), _flat(it.findtext("source", ""))
        if media and title.endswith(f" - {media}"):
            title = title[: -len(media) - 3].strip()
        when = ""
        try:
            when = f"{parsedate_to_datetime(it.findtext('pubDate', '')).astimezone(CST):%Y-%m-%d}"
        except (TypeError, ValueError):
            pass
        out.append(_item(title, it.findtext("link", ""), " ".join(filter(None, [when, media, "（新闻标题；可用 fetch_url 打开原文核实）"]))))
    return out


def _baidu(query: str, n: int) -> list[dict]:
    r = _http.get("https://www.baidu.com/s", params={"wd": query, "rn": max(n, 10)}, timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    if "安全验证" in r.text[:5000] or "wappass.baidu.com" in r.text[:5000]:
        raise Blocked("百度安全验证")
    out = []
    for c in BeautifulSoup(r.text, "html.parser").select("#content_left > div"):
        h3, url = c.select_one("h3"), c.get("mu") or ""  # mu 是真实地址
        if h3 is None or not url.startswith("http") or "nourl" in url or c.get("tpl") in ("recommend_list", "wenda_generate"):
            continue
        out.append(_item(h3.get_text(), url, c.get_text()))
        if len(out) >= n:
            break
    return out


def _bing_url(href: str) -> str:
    """Bing 的跳转链接 bing.com/ck/a?...&u=a1<base64url> 还原成真实地址。"""
    if "bing.com/ck/a" not in href:
        return href
    u = parse_qs(urlparse(href).query).get("u", [""])[0]
    if u.startswith("a1"):
        try:
            return base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode()
        except (ValueError, UnicodeDecodeError):
            pass
    return href


def _bing(query: str, n: int) -> list[dict]:
    r = _http.get("https://www.bing.com/search", params={"q": query, "count": n}, timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    out = []
    for li in BeautifulSoup(r.text, "html.parser").select("li.b_algo"):
        a = li.select_one("h2 a")
        if a is None or not a.get("href"):
            continue
        p = li.select_one(".b_caption p") or li.select_one("p")
        out.append(_item(a.get_text(), _bing_url(a["href"]), p.get_text() if p else ""))
        if len(out) >= n:
            break
    return out


def _ddg(query: str, n: int) -> list[dict]:
    r = _http.post("https://html.duckduckgo.com/html/", data={"q": query}, timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    if r.status_code == 202 or "anomaly" in r.text[:20000].lower():
        raise Blocked("DuckDuckGo 验证页")
    out = []
    for x in BeautifulSoup(r.text, "html.parser").select(".result"):
        a = x.select_one("a.result__a")
        if a is None or not a.get("href"):
            continue
        href = a["href"]
        if "duckduckgo.com/l/" in href:  # 跳转链接：真实地址在 uddg 参数里
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        sn = x.select_one(".result__snippet")
        out.append(_item(a.get_text(), href, sn.get_text() if sn else ""))
        if len(out) >= n:
            break
    return out


PROVIDERS = {"exa": _exa, "bocha": _bocha, "tavily": _tavily, "serper": _serper,
             "so360": _so360, "toutiao": _toutiao, "gnews": _gnews, "baidu": _baidu, "bing": _bing, "duckduckgo": _ddg}

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_STOP = {"the", "and", "for", "with", "of", "in", "on", "to", "an", "by", "from", "or", "vs", "site", "filetype", "pdf"}


def query_terms(query: str) -> list[str]:
    """检索式里用来判断相关性的词：中文词段（2 字以上）与英文/数字词（不含年份和常见虚词）。"""
    out = []
    for t in re.findall(r"[a-z0-9][a-z0-9.+%-]*|[\u4e00-\u9fff]+", to_simplified(query).lower()):
        t = t.rstrip(".-")
        if _CJK.fullmatch(t):
            if len(t) >= 2:
                out.append(t)
        elif len(t) >= 2 and t not in _STOP and not re.fullmatch(r"(19|20)\d\d", t):
            out.append(t)
    return out


def relevant(terms: list[str], item: dict) -> bool:
    """结果的标题、摘要或网址至少命中一个检索词才算相关。
    较长的中文词段（如「英飞凌侵权英诺赛科」）不要求整段出现，命中一半以上的相邻两字组合即可。"""
    if not terms:
        return True
    text = to_simplified(f"{item['title']} {item['snippet']} {item['url']}").lower()
    flat = re.sub(r"\s+", "", text)
    for t in terms:
        if _CJK.fullmatch(t):
            if t in flat:
                return True
            if len(t) >= 4:
                grams = {t[i:i + 2] for i in range(len(t) - 1)}
                if sum(g in flat for g in grams) * 2 >= len(grams) + 1:
                    return True
        elif re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", text):
            return True
    return False


def search(session: requests.Session | None, query: str, n: int = 8,
           providers: list[str] | None = None) -> tuple[list[dict], str]:
    """搜索 API（Exa、填了 key 的付费 API）优先；免费源中配置的前 ROTATE 个轮流使用（最久没用的先用），
    其余在它们都失败时依次备用。返回 (与检索词相关的结果, 使用的来源)；都失败时来源处是各源的失败原因。"""
    apis = api_providers()
    names = [p for p in (providers or list(PROVIDERS)) if p in apis or (p not in KEYS and p != "exa")]
    key = (_flat(query).lower(), n, tuple(names))
    if key in _cache:
        return _cache[key]
    terms = query_terms(query)
    now = time.time()
    notes = [f"{LABELS.get(p, p)}：此前被拦截，暂停中" for p in names if _blocked.get(p, 0) > now]
    paid = [p for p in names if p in apis]
    free = [p for p in names if p not in apis]
    with _lock:
        order = paid + sorted(free[:ROTATE], key=lambda p: _last.get(p, 0)) + free[ROTATE:]
    for name in order:
        label = LABELS.get(name, name)
        if _blocked.get(name, 0) > time.time():
            continue
        _pace(name)
        try:
            res = PROVIDERS[name](query, max(n, 10))
        except Blocked as e:
            _blocked[name] = time.time() + e.seconds
            if name not in apis:
                _http.cookies.clear()  # 部分站点把限流记在 cookie 上
            notes.append(f"{label}：被拦截（{e}）")
            continue
        except (requests.RequestException, ValueError, KeyError) as e:
            notes.append(f"{label}：{type(e).__name__}")
            continue
        good = [r for r in res if relevant(terms, r)]
        if good:
            with _lock:
                if len(_cache) > 500:
                    _cache.clear()
                _cache[key] = (good[:n], name)
            return good[:n], name
        notes.append(f"{label}：{'结果与检索词无关（可能被反爬）' if res else '无结果'}")
    return [], "；".join(notes) or "未配置搜索源"


def search_failed(result_text: str) -> bool:
    """web_search 工具的输出是否表示搜索服务不可用（而不是查到了东西）。"""
    return result_text.startswith("没有可用的搜索结果")


def _is_private(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "") or host.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        return False


_TOUTIAO_ID = re.compile(r"toutiao\.com/(?:a|i|article/|group/)(\d{10,})")


def _toutiao_article(url: str) -> str | None:
    """头条文章页靠脚本渲染，直接抓取没有正文；改用移动端接口取标题、发布时间和正文。"""
    m = _TOUTIAO_ID.search(url)
    if not m:
        return None
    r = _http.get(f"https://m.toutiao.com/i{m.group(1)}/info/", timeout=FETCH_TIMEOUT)
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    if not isinstance(data, dict) or not data.get("content"):
        return None
    text = BeautifulSoup(data["content"], "html.parser").get_text("\n", strip=True)
    head = f"标题：{_flat(data.get('title', ''))}\n"
    if str(data.get("publish_time", "")).isdigit():
        head += f"发布时间：{datetime.fromtimestamp(int(data['publish_time']), CST):%Y-%m-%d %H:%M}\n"
    if isinstance(data.get("source"), str) and data["source"]:
        head += f"来源：{data['source']}\n"
    return head + text


def _gnews_url(link: str) -> str | None:
    """Google 新闻 RSS 的文章链接是加密的跳转地址：先取页面里的签名，再调用 batchexecute 换出原文地址。"""
    gid = urlparse(link).path.rstrip("/").rsplit("/", 1)[-1]
    page = _http.get(f"https://news.google.com/rss/articles/{gid}", timeout=FETCH_TIMEOUT).text
    sg, ts = re.search(r'data-n-a-sg="([^"]+)"', page), re.search(r'data-n-a-ts="(\d+)"', page)
    if not (sg and ts):
        return None
    inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                            "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], gid, int(ts.group(1)), sg.group(1)]
    r = _http.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                   data={"f.req": json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])},
                   headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}, timeout=FETCH_TIMEOUT)
    for line in r.text.splitlines():
        if line.startswith('[["wrb.fr"'):
            payload = json.loads(json.loads(line)[0][2])
            if payload and payload[0] == "garturlres" and str(payload[1]).startswith("http"):
                return payload[1]
    return None


def exa_fetch(url: str, max_chars: int = 6000) -> str | None:
    """用 Exa 读取网页（Exa 在自己的服务器上渲染页面，适合脚本渲染或拒绝程序访问的网站）。不可用时返回 None。"""
    if not (os.environ.get("EXA_API_KEY") or MCPORTER) or _blocked.get("exa_fetch", 0) > time.time() or _is_private(url):
        return None
    try:
        if os.environ.get("EXA_API_KEY"):
            res = (_exa_rest("contents", {"urls": [url], "text": {"maxCharacters": max_chars}}).get("results") or [{}])[0]
            text = (f"标题：{res['title']}\n" if res.get("title") else "") + (res.get("text") or "").strip()
        else:
            text = _exa_call("web_fetch_exa", {"urls": [url], "maxCharacters": max_chars}).strip()
    except Blocked as e:
        _blocked["exa_fetch"] = time.time() + e.seconds
        return None
    except (requests.RequestException, ValueError):
        return None
    return f"（经 Exa 读取）\n{text}" if len(text) > 200 else None


def fetch_text(session: requests.Session, url: str, max_chars: int = 6000) -> str:
    """抓取网页或 PDF 并返回正文文本。不允许访问本机和内网地址。"""
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("只支持 http/https 地址")
    if (urlparse(url).hostname or "") == "news.google.com" and "/articles/" in url:
        real = _gnews_url(url)
        if not real:
            raise ValueError("无法解析 Google 新闻链接的原文地址；可以用标题再搜索一次")
        url = real
    if _is_private(url):
        raise ValueError("不允许访问本机或内网地址")
    host = urlparse(url).hostname or ""
    if host.endswith("toutiao.com"):
        text = _toutiao_article(url)
        if text:
            return text if len(text) <= max_chars else text[:max_chars] + f"\n……（已截断，原文约 {len(text):,} 字）"
    if host.endswith(RENDERED_SITES) and (text := exa_fetch(url, max_chars)):
        return text
    try:
        r = _http.get(url, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException:
        if text := exa_fetch(url, max_chars):  # 被拒绝访问、超时等：改由 Exa 读取
            return text
        raise
    ctype = r.headers.get("Content-Type", "").lower()
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf") or r.content[:5] == b"%PDF-":
        import pymupdf

        with pymupdf.open(stream=r.content, filetype="pdf") as doc:
            parts, total = [], 0
            for i, page in enumerate(doc, 1):
                t = page.get_text("text").strip()
                parts.append(f"[第{i}页]\n{t}")
                total += len(t)
                if total > max_chars:
                    break
            text = "\n\n".join(parts)
            head = f"（PDF，共 {doc.page_count} 页）\n"
    else:
        r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        title = clean_text(soup.title.get_text()) if soup.title else ""
        text = re.sub(r"\n\s*\n+", "\n", soup.get_text("\n", strip=True))
        head = f"标题：{title}\n" if title else ""
        if len(text) < 300 and (rendered := exa_fetch(url, max_chars)):  # 正文靠脚本渲染
            return rendered
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n……（已截断，原文约 {len(text):,} 字）"
    return head + text
