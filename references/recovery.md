# Chrome 会话与静默媒体获取

默认 `auto_browser_recovery=false`。作者清单和逐条材料获取都只读现有 Chrome 会话、缓存与后台接口，不打开作品页、不启动 Chrome、不要求保持抖音标签页打开。不得为抓取失败自行改成可见浏览器恢复。

## 先检查已有会话

`configure.py`、`dy_author.py init` 先检查真实操作系统用户的 Chrome 资料；即使 zcode 改写 HOME，也不使用应用沙箱中的假 Chrome 路径。未指定 browser_profile 时，按 Chrome Local State 的近期资料顺序检查，优先选择能读到抖音会话的资料；Cookie 与缓存绑定到同一 profile。指定 profile 时遵守该路径，不自动切换。Cookie 只在内存中使用，不显示、不保存。

- `available`：读到未过期的抖音会话标记，直接复用；服务器是否接受仍由后续请求确认。
- `login_required`：没有读到会话。提示用户手动在对应 Chrome 资料中打开 https://www.douyin.com/ 登录后重试，不自动弹页。
- `profile_unavailable`：找不到 Chrome 资料。提示先使用 Chrome 登录抖音；自定义资料需配置完整 browser_profile。
- `read_error`：读取授权、Cookie 数据库或路径出现问题。只报告错误类别，请用户检查读取权限和资料路径；不能断言已退出登录。

`plan/next/prepare-all` 会重检会话；条件未满足时在准备前停止并保留队列，不把待处理作品批量记为失败。commit 与已有草稿不依赖这一步；此前已保存的笔记不会回滚。当前配置关闭自动开页时，旧批次快照中的 true 也不能启用它。

## 后台获取

正常队列无需额外操作：

1. 优先复用哈希和完整音轨时长已校验的材料；否则只读 Chrome 收到的官方详情缓存。请求与响应作品 ID 必须一致，缓存 CRC、key SHA、解压完整性必须通过。Simple Index 仅作定位线索；未知、损坏或截断索引回退原查找，不修改浏览器文件。
2. 没有匹配缓存时，用内存 CookieJar 调固定版本 F2；必要时只在该请求进程内访问官方页面接收 Set-Cookie，并再调用一次 F2。这里不执行网页 JavaScript，不更新 Chrome Cookie 数据库。
3. 必要时用独立匿名会话读取公开移动分享页，仅接受 JSON 中严格匹配作品 ID 的完整详情。视频只下载该作品 video 字段中的播放地址，图文下载完整图片，不把配乐、封面或推荐作品当原材料。
4. 显式 HTTP 429 停止当前恢复；网络超时、403、空详情或 yt-dlp 的 Fresh cookies 提示本身均不能证明登录失效。默认后台路线失败后返回具体原因，不开页面，也不循环重试。

视频音轨还须解码验收，和详情时长的容差最大为 3 秒或 2%；缺少完整材料不能按标题或模型常识入库。HTTP 恢复记录仅保存作品 ID、路由、时间和结果代码，不保存 Cookie、签名媒体 URL 或完整响应。

## 仅用户明确启用的可见恢复

只有用户明确要求时才在当前配置设置 `auto_browser_recovery=true`。`prefer_browser_media=false`、`background_detail_first=true` 仍使后台请求优先；可见恢复每条最多一次，打开前把 attempted 写入 state.json。以同一完整 profile 传入 Chrome 的 user-data-dir/profile-directory，最多等待配置的缓存预算；默认 browser_recovery_wait_seconds=8、browser_media_timeout_seconds=40。仅尝试关闭本次打开、严格匹配作品 ID 的新增标签；已有标签保留，未取得自动化授权时不关闭。

关闭新增标签不等于没有弹窗，所以默认保持 false。脚本不点击、不输入、不操作登录或验证码。`page_state=not_observed` 只表示未观察实际页面，不能称已登录、已播放或已刷新到有效 Cookie。下载阶段失败保留 download 原因，不能用后续详情失败覆盖。

## 区分缓存查找与作品故障

查看 cache_baseline_entries、cache_probe.scan_incomplete/deadline_reached 与 chrome_index_status。扫描未完成或超时应返回 CACHE_LOOKUP_INCOMPLETE；不能据此判作品无媒体、建议重新登录或等几天。mtime 也不能单独证明响应何时完整落盘。保留原批次和失败原因，不另建循环、不按页面摘要补写笔记。

只有实际观察到登录/验证码、已删除、私密或无权限页面时，才报告对应状态。页面能播放但后台无法取得完整媒体时，排查提取适配；会话标记可读不保证接口始终可用。
