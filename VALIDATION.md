# 验证方式与范围

在全新的 Python 3.12 虚拟环境中安装本仓库的 requirements.lock.txt，再运行 pip check 和完整 unittest：

~~~sh
python -m pip install -r requirements.lock.txt
python -m pip check
python -B -m unittest discover -s tests -v
~~~

测试使用临时 Vault、合成材料和显式替代的外部接口，覆盖显式命令范围、作者分页与归属、游标、视频与图文、原始材料补录、全库查重、作者文档、面试题库、通知去重、事务回滚和安装配置。测试只加载本仓库的脚本。

知识总索引和导入总日志由本仓库维护。日志摘要显示总记录数，历史来源单元格仍被保留。笔记、题目文档和总览在本仓库的锁与事务中提交。

离线回归、依赖安装与实际在线导入分别判断。单元测试不能证明抖音接口、账号权限或 Cookie 当前可用，也不能证明任意素材的转写和识别质量。doctor 只检查本机配置和依赖发现。

本文给出当前源码的验证方法；具体测试结果以对应提交或 Release 的运行记录为准。

完整功能面向 macOS Apple Silicon。其他平台的在线导入与识别不在当前验证承诺中。

## 2026-10-26：Chrome 会话与默认静默

在固定的 Python 3.12.14 环境下，针对本次修改运行 `test_chrome_session`、`test_configure`、`test_douyin_access`、`test_author_works`、`test_dy_author`、`test_invocation_scope`：84 项通过，无失败、错误或跳过。Skill 的 `quick_validate.py` 通过。

覆盖 zcode 改写 HOME 后仍定位真实用户资料、多个 profile 中复用抖音会话、缓存资料匹配、缺少/过期会话提示、读取失败脱敏、首次配置默认不弹页、默认详情失败不启动浏览器，以及当前关闭设置约束旧批次且保留已保存条目。可见恢复测试显式配置 `auto_browser_recovery=true`。

会话检查仅判断本机是否读到未过期标记，不验证服务器是否接受，也不构成真实作者全量下载通过。接口拒绝、缓存查找不完整与缺少登录会话仍分别判断。
