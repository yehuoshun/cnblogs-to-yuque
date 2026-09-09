# cnblogs-to-yuque

博客园文章 → 语雀知识库 自动采集（GitHub Actions 定时运行）。

## 工作链路

```
RSS 吐文章 URL → 抓详情页全文(#cnblogs_post_body) → HTML→markdown
→ 图片下载+上传语雀 CDN → 语雀 v2 建文档 → 挂「作者」目录 → state.json 去重
```

- 去重：`state.json` 存仓库，URL 做 key，**建文档成功才标记**（失败下次重试）
- 图片：下载博客园图片 → `POST /api/upload/attach` 传语雀 CDN（`cdn.nlark.com`），失败降级保留博客园原 URL
- 长文：纯文本超 200KB 按标题切（无标题按段落切），命名 `标题-N`
- 原文：每篇文末附「原文链接」

## 配置

### 1. 目标源（config.json 的 `feeds`）

```json
{
  "feeds": [
    { "type": "sitehome" },
    { "type": "user", "param": "某博主用户名" }
  ]
}
```

| type | 说明 | param |
|---|---|---|
| `sitehome` | 首页最新 | 无 |
| `picked` | 编辑推荐 | 无 |
| `48h` | 48 小时阅读排行 | 无 |
| `10d` | 10 天推荐排行 | 无 |
| `user` | 指定博主 | 用户名 |
| `category` | 分类 | 分类名 |

### 2. 目标知识库

- `config.json` 的 `book` 填语雀 namespace（如 `yehuoshun/xxx`）
- 或用 GitHub Secret `YUQUE_BOOK`

### 3. GitHub Secrets（敏感信息）

| Secret | 值 |
|---|---|
| `YUQUE_TOKEN` | 语雀 X-Auth-Token（建文档） |
| `YUQUE_COOKIE` | 浏览器登录语雀后的 `_yuque_session` 相关 Cookie（传图） |
| `YUQUE_CTOKEN` | 语雀 `yuque_ctoken`（传图） |
| `DINGTALK_WEBHOOK` | 钉钉机器人 access_token（失败告警，可选） |
| `YUQUE_BOOK` | 目标知识库（可选，覆盖 config） |

## 运行

- **自动**：每天北京时间凌晨 3:00（UTC 19:00）
- **手动**：仓库 Actions 页 → `cnblogs-crawl` → Run workflow

## 注意事项

- 语雀 cookie 约 2-3 月失效，失效后钉钉会告警，需重新 F12 复制 Cookie 更新 secret
- 语雀限流 5000 请求/小时，脚本逐条间隔 1s，远低于上限
- 博客园有反爬，脚本内置 UA 轮换 + 重试 + 降频
- 上传图片必须带 `Referer: https://www.yuque.com/`，否则 400
