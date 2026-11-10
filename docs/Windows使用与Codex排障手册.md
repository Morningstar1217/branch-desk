# Branch Desk Windows 使用说明

适用 Windows 10/11 x64，安装到当前用户的 `%LOCALAPPDATA%\Programs\Branch Desk`。Python 与 .NET 已随包提供，电脑需安装 Git for Windows 和 Microsoft Edge WebView2 Runtime。

1. 从桌面或开始菜单打开 Branch Desk。
2. 点击“添加本地项目…”，选择已经克隆到本机的 Git 仓库目录。
3. 点击“启动 / 连接工作台”，查看分支、差异和本地工作区。
4. 前端、Java 服务和飞书关联按项目单独配置；Node、JDK、Codex CLI 及对应账号按需要准备。

关闭窗口会保留工作台后台。升级前，从“窗口”菜单关闭所有空闲工作台后台，再关闭窗口。业务服务需另行停止。Windows 设置的“已安装的应用”中可以卸载 Branch Desk；仓库及用户配置会保留。

配置和日志位于 `%LOCALAPPDATA%\Branch Desk`。不要分享含会话凭证的完整本地 URL。排障时提供错误文字、操作步骤和去除凭证后的日志。启动失败时先检查 Git 是否在 PATH、所选目录是否为 Git 仓库，以及 WebView2 Runtime 是否存在。飞书和远程 Git 操作需要你自己的配置和权限。

本次实际验证范围请参阅“发布验证说明.md”。
