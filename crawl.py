#!/usr/bin/env python3
"""
博客园 → 语雀 爬虫

GitHub Actions 定时运行：RSS 吐文章 URL → 抓详情页全文 → HTML→markdown →
图片下载+上传语雀 CDN → 语雀 v2 建文档 → 按「作者」目录归类 → state.json 去重。

敏感信息全部走环境变量（GitHub Secrets）：
  YUQUE_TOKEN   X-Auth-Token（建文档）
  YUQUE_COOKIE  _yuque_session（传图）
  YUQUE_CTOKEN  ctoken（传图）
  DINGTALK_WEBHOOK  钉钉机器人 webhook（可选，失败告警）
  YUQUE_BOOK    目标知识库 namespace/ID（可选，覆盖 config）
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse

import requests
import feedparser
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
API_BASE = "https://www.yuque.com/api/v2"
UPLOAD_URL = "https://www.yuque.com/api/upload/attach"
YUQUE_USER_ID = 25689388  # 语雀 user_id，图片上传 attachable_id 用
API_MIN_INTERVAL = 0.5     # 语雀 API 最小请求间隔（秒），防限流

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")
STATE_PATH = "state.json"

FEED_URLS = {
    "sitehome": "https://feed.cnblogs.com/blog/sitehome/rss",       # 首页最新
    "picked": "https://feed.cnblogs.com/blog/sitehome/picked",      # 编辑推荐
    "48h": "https://feed.cnblogs.com/blog/sitehome/48h",            # 48小时阅读排行
    "10d": "https://feed.cnblogs.com/blog/sitehome/10d",            # 10天推荐排行
    "user": "https://feed.cnblogs.com/blog/u/{param}/rss",          # 指定博主
    "category": "https://feed.cnblogs.com/blog/category/{param}/rss",  # 分类
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 配置 / 状态
# ---------------------------------------------------------------------------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"processed": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"processed": {}}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def env(name, default=""):
    return os.environ.get(name, default)


# ---------------------------------------------------------------------------
# 钉钉告警
# ---------------------------------------------------------------------------
def notify_dingtalk(text):
    token = env("DINGTALK_WEBHOOK")
    if not token:
        log("未配置 DINGTALK_WEBHOOK，跳过告警")
        return
    url = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
    # 钉钉机器人设了安全关键词「GitHub」，消息必须包含该词
    content = text if "GitHub" in text else f"GitHub {text}"
    try:
        r = requests.post(url, json={"msgtype": "text", "text": {"content": content}}, timeout=10)
        data = r.json()
        if data.get("errcode") == 0:
            log("钉钉告警已发送")
        else:
            log(f"钉钉告警失败: {data.get('errmsg')}")
    except Exception as e:
        log(f"钉钉告警发送失败: {e}")


# ---------------------------------------------------------------------------
# 语雀请求：限流 + 429/5xx 退避重试
# ---------------------------------------------------------------------------
_last_api_call = 0.0


def _throttle():
    """保证语雀 API 请求间隔 ≥ API_MIN_INTERVAL"""
    global _last_api_call
    now = time.time()
    wait = API_MIN_INTERVAL - (now - _last_api_call)
    if wait > 0:
        time.sleep(wait)
    _last_api_call = time.time()


def _handle_429(r, attempt, retries, label="语雀"):
    """429 区分两种：瞬时限流（QPS）短退避；小时配额耗尽（X-RateLimit-Remaining=0）等整点"""
    remaining = r.headers.get("X-RateLimit-Remaining", "")
    if remaining == "0":
        now = time.localtime()
        secs = 3600 - (now.tm_min * 60 + now.tm_sec)
        log(f"{label} 429：小时配额耗尽，等 {secs}s 到整点重置")
        time.sleep(secs)
        return
    wait = attempt + 1  # 1s / 2s / 3s
    log(f"{label} 429 瞬时限流，退避 {wait}s 重试({attempt + 1}/{retries})")
    time.sleep(wait)


def api_request(method, url, token=None, json_body=None, retries=4):
    """带限流 + 退避重试的语雀请求；返回最后一次响应"""
    headers = {"User-Agent": USER_AGENTS[0]}
    if token:
        headers["X-Auth-Token"] = token
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    r = None
    for attempt in range(retries):
        _throttle()
        r = requests.request(method, url, headers=headers, json=json_body, timeout=30)
        if r.status_code == 429:
            _handle_429(r, attempt, retries)
            continue
        if r.status_code >= 500:
            wait = 3 * (attempt + 1)
            log(f"语雀 {r.status_code} 服务端错误，退避 {wait}s 重试({attempt + 1}/{retries})")
            time.sleep(wait)
            continue
        return r
    return r


# ---------------------------------------------------------------------------
# HTTP 抓取
# ---------------------------------------------------------------------------
def fetch_text(url, timeout=20, referer=None, retries=3):
    last_err = None
    for i in range(retries):
        headers = {"User-Agent": USER_AGENTS[i % len(USER_AGENTS)]}
        if referer:
            headers["Referer"] = referer
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            log(f"抓取失败({i+1}/{retries}) {url}: {e}")
            time.sleep(2 * (i + 1))
    raise last_err


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------
def fetch_articles(feed_cfg):
    """从 RSS 返回文章列表 [{url, title, author}]"""
    ftype = feed_cfg.get("type")
    param = feed_cfg.get("param", "")
    if ftype not in FEED_URLS:
        raise ValueError(f"未知 feed 类型: {ftype}")
    url = FEED_URLS[ftype].format(param=urllib.parse.quote(param)) if "{param}" in FEED_URLS[ftype] \
        else FEED_URLS[ftype]

    log(f"抓取 RSS: {url}")
    feed = feedparser.parse(url)
    articles = []
    for entry in feed.entries:
        link = entry.get("link") or entry.get("id") or ""
        title = (entry.get("title") or "").strip()
        author = ""
        if entry.get("author"):
            author = entry.author if isinstance(entry.author, str) else entry.author.get("name", "")
        if not link:
            continue
        articles.append({"url": link, "title": title, "author": author})
    log(f"RSS 共 {len(articles)} 篇")
    return articles


# ---------------------------------------------------------------------------
# 详情页提取 + HTML→markdown
# ---------------------------------------------------------------------------
def extract_body_html(html):
    """提取正文 HTML（博客园正文容器 #cnblogs_post_body）"""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#cnblogs_post_body")
    if not body:
        return ""
    # 去掉正文里的脚本/样式
    for tag in body(["script", "style", "noscript"]):
        tag.decompose()
    return str(body)


def html_to_markdown(html):
    """HTML 正文 → markdown（博客园风格）"""
    soup = BeautifulSoup(html, "html.parser")

    def walk(node):
        out = []
        for child in node.children:
            name = getattr(child, "name", None)
            if name is None:  # 文本
                txt = str(child)
                if txt.strip():
                    out.append(txt)
                continue

            if name == "br":
                out.append("\n")
            elif name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                level = int(name[1])
                out.append("\n\n" + "#" * level + " " + _inline(child) + "\n\n")
            elif name == "p":
                out.append("\n\n" + _inline(child) + "\n\n")
            elif name == "pre":
                code = child.find("code")
                lang = ""
                if code and code.get("class"):
                    for c in code["class"]:
                        if c.startswith("language-"):
                            lang = c.replace("language-", "")
                text = code.get_text() if code else child.get_text()
                out.append(f"\n\n```{lang}\n{text}\n```\n\n")
            elif name == "blockquote":
                inner = walk(child).strip()
                out.append("\n\n> " + inner.replace("\n", "\n> ") + "\n\n")
            elif name in ("ul", "ol"):
                out.append("\n\n" + _list(child, name == "ol") + "\n\n")
            elif name == "table":
                out.append("\n\n" + _table(child) + "\n\n")
            elif name == "hr":
                out.append("\n\n---\n\n")
            elif name == "img":
                src = child.get("src", "")
                alt = child.get("alt", "")
                if src:
                    out.append(f"\n\n![{alt}]({src})\n\n")
            elif name in ("strong", "b"):
                out.append("**" + _inline(child) + "**")
            elif name in ("em", "i"):
                out.append("*" + _inline(child) + "*")
            elif name in ("code",):
                out.append("`" + child.get_text() + "`")
            elif name == "a":
                href = child.get("href", "")
                txt = _inline(child)
                if href:
                    out.append(f"[{txt}]({href})")
                else:
                    out.append(txt)
            else:
                out.append(walk(child))
        return "".join(out)

    def _inline(node):
        parts = []
        for child in node.children:
            n = getattr(child, "name", None)
            if n is None:
                parts.append(str(child))
            elif n in ("strong", "b"):
                parts.append("**" + _inline(child) + "**")
            elif n in ("em", "i"):
                parts.append("*" + _inline(child) + "*")
            elif n == "code":
                parts.append("`" + child.get_text() + "`")
            elif n == "a":
                href = child.get("href", "")
                parts.append(f"[{_inline(child)}]({href})" if href else _inline(child))
            elif n == "img":
                src = child.get("src", "")
                alt = child.get("alt", "")
                parts.append(f"![{alt}]({src})" if src else "")
            elif n == "br":
                parts.append(" ")
            else:
                parts.append(_inline(child))
        return "".join(parts)

    def _list(node, ordered):
        items = []
        for li in node.find_all("li", recursive=False):
            items.append("- " if not ordered else "1. ")
            items.append(_inline(li).strip() + "\n")
        return "".join(items)

    def _table(node):
        rows = node.find_all("tr")
        md = []
        for ri, tr in enumerate(rows):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            md.append("| " + " | ".join(cells) + " |")
            if ri == 0:
                md.append("| " + " | ".join(["---"] * len(cells)) + " |")
        return "\n".join(md)

    raw = walk(soup)
    # 清理多余空行
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def extract_title(html):
    soup = BeautifulSoup(html, "html.parser")
    # 优先取正文纯标题（不含作者名/博客园后缀）
    t = soup.select_one("#cb_post_title_url")
    if t:
        title = t.get_text().strip()
        if title:
            return title
    t = soup.find("title")
    title = t.get_text().strip() if t else ""
    # 去掉 " - 作者名 - 博客园" 之类的后缀
    title = re.sub(r"\s*[-|_]\s*博客园.*$", "", title).strip()
    return title


# ---------------------------------------------------------------------------
# 图片处理
# ---------------------------------------------------------------------------
def upload_image(image_bytes, ext, cookie, ctoken, retries=3):
    """上传图片到语雀 CDN，返回 cdn URL；429 退避重试，其他失败返回 None"""
    params = {
        "attachable_type": "User",
        "attachable_id": YUQUE_USER_ID,
        "type": "image",
        "ctoken": ctoken,
    }
    headers = {
        "Cookie": cookie,
        "Referer": "https://www.yuque.com/",
        "User-Agent": USER_AGENTS[0],
    }
    mime = f"image/{ext}" if ext != "jpg" else "image/jpeg"
    files = {"file": (f"img.{ext}", image_bytes, mime)}
    for attempt in range(retries):
        _throttle()
        try:
            r = requests.post(UPLOAD_URL, params=params, headers=headers, files=files, timeout=30)
            if r.status_code == 429:
                _handle_429(r, attempt, retries, label="图片上传")
                continue
            if r.status_code == 200:
                data = r.json().get("data", {})
                url = data.get("url")
                if url:
                    return url
            log(f"图片上传失败 HTTP {r.status_code}: {r.text[:200]}")
            return None
        except Exception as e:
            log(f"图片上传异常: {e}")
            return None
    return None


def process_images(markdown, cookie, ctoken):
    """把 markdown 里的外链图片下载+上传到语雀 CDN，替换 URL"""
    def repl(m):
        alt = m.group(1)
        src = m.group(2)
        if src.startswith("data:") or "cdn.nlark.com" in src:
            return m.group(0)
        try:
            r = fetch_text(src, timeout=15, referer="https://www.cnblogs.com/", retries=2)
            ext = (src.split(".")[-1].split("?")[0]).lower()
            if ext not in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
                ext = "png"
            new_url = upload_image(r.content, ext, cookie, ctoken)
            if new_url:
                return f"![{alt}]({new_url})"
            log(f"图片降级用原 URL: {src[:80]}")
        except Exception as e:
            log(f"图片下载失败，降级原 URL: {src[:80]} ({e})")
        return m.group(0)  # 失败降级：保留博客园原 URL

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl, markdown)


# ---------------------------------------------------------------------------
# 长文截断
# ---------------------------------------------------------------------------
def split_markdown(markdown, max_bytes):
    """按 ## 标题切块；无标题则按段落累积，控制每块 ≤ max_bytes"""
    if len(markdown.encode("utf-8")) <= max_bytes:
        return [markdown]

    # 按二级标题切
    parts = re.split(r"(?m)^(##\s+.+)$", markdown)
    # parts[0] 是开头无标题部分，之后是 (标题, 内容) 交替
    chunks = []
    buf = parts[0] if parts else ""
    i = 1
    while i < len(parts):
        heading = parts[i]
        content = parts[i + 1] if i + 1 < len(parts) else ""
        i += 2
        block = heading + "\n" + content
        if len((buf + block).encode("utf-8")) > max_bytes and buf.strip():
            chunks.append(buf)
            buf = block
        else:
            buf += block
    if buf.strip():
        chunks.append(buf)

    # 仍有超限块（无标题长文），按段落硬切
    final = []
    for c in chunks:
        if len(c.encode("utf-8")) <= max_bytes:
            final.append(c)
            continue
        paras = c.split("\n\n")
        cur = ""
        for p in paras:
            if len((cur + p).encode("utf-8")) > max_bytes and cur.strip():
                final.append(cur)
                cur = p
            else:
                cur += "\n\n" + p
        if cur.strip():
            final.append(cur)
    return final


# ---------------------------------------------------------------------------
# 语雀操作
# ---------------------------------------------------------------------------
def create_doc(book, title, slug, body, token):
    """创建文档，返回 doc_id"""
    r = api_request("POST", f"{API_BASE}/repos/{book}/docs", token=token,
                    json_body={"title": title, "slug": slug, "body": body, "format": "markdown"})
    if r is not None and r.status_code in (200, 201):
        return r.json()["data"]["id"]
    code = getattr(r, "status_code", "?")
    text = getattr(r, "text", "")[:300]
    raise RuntimeError(f"建文档失败 HTTP {code}: {text}")


def get_toc(book, token):
    r = api_request("GET", f"{API_BASE}/repos/{book}/toc", token=token)
    if r.status_code != 200:
        raise RuntimeError(f"获取目录失败 HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("data", [])


def update_toc(book, token, payload):
    r = api_request("PUT", f"{API_BASE}/repos/{book}/toc", token=token, json_body=payload)
    if r.status_code != 200:
        raise RuntimeError(f"更新目录失败 HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("data", [])


_toc_cache = {"book": None, "nodes": None}


def get_cached_toc(book, token):
    """获取目录（本次运行内缓存，避免每次挂载都拉全量 TOC）"""
    if _toc_cache["book"] != book or _toc_cache["nodes"] is None:
        _toc_cache["book"] = book
        _toc_cache["nodes"] = get_toc(book, token)
    return _toc_cache["nodes"]


def get_or_create_author_node(book, author, token):
    """找到或创建「作者」目录节点，返回其 uuid"""
    toc = get_cached_toc(book, token)
    for node in toc:
        if node.get("type") == "TITLE" and node.get("title") == author:
            return node["uuid"]
    # 创建作者分组
    new_toc = update_toc(book, token, {
        "action": "appendNode",
        "action_mode": "child",
        "type": "TITLE",
        "title": author,
    })
    _toc_cache["nodes"] = new_toc
    for node in new_toc:
        if node.get("type") == "TITLE" and node.get("title") == author:
            return node["uuid"]
    raise RuntimeError(f"创建作者目录失败: {author}")


def attach_to_author(book, author_uuid, doc_id, token):
    new_toc = update_toc(book, token, {
        "action": "appendNode",
        "action_mode": "child",
        "type": "DOC",
        "doc_ids": [doc_id],
        "target_uuid": author_uuid,
    })
    _toc_cache["nodes"] = new_toc


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    cfg = load_config()
    # 敏感信息只走环境变量（GitHub Secrets），绝不从 config.json 读，防误提交进 public 仓库泄露
    token = env("YUQUE_TOKEN")
    cookie = env("YUQUE_COOKIE")
    ctoken = env("YUQUE_CTOKEN")
    book = env("YUQUE_BOOK") or cfg.get("book", "") or cfg.get("book_id", "")
    feeds = cfg.get("feeds", [])
    max_bytes = int(cfg.get("max_text_bytes", 200 * 1024))
    interval = float(cfg.get("request_interval", 1.0))

    if not token:
        log("缺少 YUQUE_TOKEN")
        sys.exit(1)
    if not feeds:
        log("config 里未配置 feeds（目标源缺省），本轮跳过")
        return
    if not book:
        log("目标知识库缺省（未配置 book/YUQUE_BOOK），本轮跳过")
        return

    state = load_state()
    processed = state.setdefault("processed", {})
    author_cache = {}  # 作者目录 uuid 缓存，避免每篇重复 get_toc
    new_count = 0
    fail_count = 0
    cookie_ok = True
    empty_body_streak = 0

    for feed_cfg in feeds:
        try:
            articles = fetch_articles(feed_cfg)
        except Exception as e:
            log(f"RSS 失败: {e}")
            continue

        for art in articles:
            url = art["url"]
            if url in processed:
                continue
            log(f"处理: {url}")
            time.sleep(interval)
            try:
                # 1. 抓详情页
                r = fetch_text(url, timeout=20, referer="https://www.cnblogs.com/")
                title = extract_title(r.text) or art["title"]
                author = art["author"] or "未分类"
                body_html = extract_body_html(r.text)
                if not body_html:
                    empty_body_streak += 1
                    log(f"  正文为空，跳过: {url}（连续 {empty_body_streak} 篇）")
                    if empty_body_streak >= 5:
                        notify_dingtalk("[cnblogs-to-yuque] ⚠️ 连续 5 篇正文为空，疑似被博客园反爬，请检查")
                        empty_body_streak = 0
                    continue
                empty_body_streak = 0
                markdown = html_to_markdown(body_html)
                if not markdown.strip():
                    log(f"  正文转换后为空，跳过: {url}")
                    continue

                # 2. 图片下载+上传
                if cookie and ctoken:
                    markdown = process_images(markdown, cookie, ctoken)

                # 3. 文末原文链接
                markdown += f"\n\n---\n\n原文链接：{url}\n"

                # 4. 长文截断
                chunks = split_markdown(markdown, max_bytes)

                # 5. 建文档 + 挂目录
                base_slug = hashlib.md5(url.encode()).hexdigest()[:12]
                for idx, chunk in enumerate(chunks):
                    doc_title = title if len(chunks) == 1 else f"{title}-{idx + 1}"
                    doc_slug = base_slug if len(chunks) == 1 else f"{base_slug}-{idx + 1}"
                    doc_id = create_doc(book, doc_title, doc_slug, chunk, token)
                    if author not in author_cache:
                        author_cache[author] = get_or_create_author_node(book, author, token)
                    attach_to_author(book, author_cache[author], doc_id, token)
                    log(f"  ✅ 建文档 #{doc_id} 《{doc_title}》")

                # 6. 标记已处理（全部成功才标记）
                processed[url] = {"title": title, "author": author, "ts": int(time.time())}
                new_count += 1
            except Exception as e:
                fail_count += 1
                log(f"  ❌ 失败: {url} ({e})")
                # cookie 失效探测：上传阶段身份错误
                if "invalid" in str(e).lower() or "401" in str(e) or "403" in str(e):
                    cookie_ok = False

    # 每次跑都更新 last_run，保证 state.json 有 diff → commit → 保活 schedule
    state["last_run"] = int(time.time())
    save_state(state)
    summary = f"博客园→语雀 本轮完成：新增 {new_count} 篇，失败 {fail_count} 篇"
    log(summary)
    if fail_count > 0:
        notify_dingtalk(f"[cnblogs-to-yuque] {summary}")
    if not cookie_ok:
        notify_dingtalk("[cnblogs-to-yuque] ⚠️ 语雀 cookie 可能已失效，请更新 YUQUE_COOKIE/YUQUE_CTOKEN secret")


if __name__ == "__main__":
    main()
