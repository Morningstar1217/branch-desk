using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal static class Program
{
    [STAThread]
    static void Main()
    {
        ApplicationConfiguration.Initialize();
        Application.ThreadException += (_, e) => MessageBox.Show(e.Exception.Message, "Branch Desk");
        Application.Run(new DeskWindow());
    }
}

internal sealed record Project(string Id, string Name, string Path)
{
    public override string ToString() => $"{Name} — {Path}";
}

internal sealed class DeskWindow : Form
{
    readonly string root = AppContext.BaseDirectory;
    readonly Panel welcome = new() { Dock = DockStyle.Fill, Padding = new Padding(36) };
    readonly ComboBox projects = new() { DropDownStyle = ComboBoxStyle.DropDownList, Width = 720 };
    readonly Label message = new() { AutoSize = true, MaximumSize = new Size(820, 0), Margin = new Padding(0, 20, 0, 0) };
    readonly Button add = new() { Text = "添加本地项目…", AutoSize = true };
    readonly Button connect = new() { Text = "启动 / 连接工作台", AutoSize = true };
    readonly ToolStripMenuItem home = new("选择项目 / 重新连接");
    readonly ToolStripMenuItem stop = new("关闭所有空闲工作台后台…");
    WebView2? web;
    bool busy, loaded, allowClose, checkingClose;

    public DeskWindow()
    {
        Text = "Branch Desk · 本地开发工作台";
        Width = 1360; Height = 900; MinimumSize = new Size(800, 560);
        StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Microsoft YaHei UI", 10);
        var menu = new MenuStrip();
        var window = new ToolStripMenuItem("窗口");
        window.DropDownItems.AddRange([home, stop]);
        var guide = new ToolStripMenuItem("使用与 Codex 排障手册");
        guide.Click += (_, _) => OpenDocument("Windows使用与Codex排障手册.html");
        menu.Items.AddRange([window, guide]);
        MainMenuStrip = menu;
        var layout = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown, WrapContents = false, AutoScroll = true };
        layout.Controls.Add(new Label { Text = "连接你的项目", AutoSize = true, Font = new Font(Font.FontFamily, 22, FontStyle.Bold), Margin = new Padding(0, 0, 0, 14) });
        layout.Controls.Add(new Label { Text = "先安装 Git for Windows，再选择已经克隆到本机的 Git 仓库。", AutoSize = true, Margin = new Padding(0, 0, 0, 18) });
        layout.Controls.Add(projects);
        var actions = new FlowLayoutPanel { AutoSize = true, Margin = new Padding(0, 18, 0, 0) };
        actions.Controls.AddRange([add, connect]);
        layout.Controls.Add(actions);
        layout.Controls.Add(message);
        welcome.Controls.Add(layout);
        Controls.Add(welcome); Controls.Add(menu);
        add.Click += async (_, _) => await AddProject();
        connect.Click += async (_, _) => await Connect();
        home.Click += async (_, _) => { if (!await CanLeave()) return; ShowWelcome(); await LoadProjects(); };
        stop.Click += async (_, _) => await StopBackends();
        Shown += async (_, _) => await LoadProjects();
        FormClosing += OnWindowClosing;
    }

    void SetBusy(bool value)
    {
        busy = value;
        add.Enabled = connect.Enabled = projects.Enabled = home.Enabled = stop.Enabled = !value;
        UseWaitCursor = value;
    }

    async Task<JsonElement> Bridge(params string[] arguments)
    {
        string python = Path.Combine(root, "python", "python.exe");
        string script = Path.Combine(root, "app", "windows", "bridge.py");
        if (!File.Exists(python) || !File.Exists(script)) throw new IOException("安装文件不完整，请使用完整安装包重新安装。");
        var start = new ProcessStartInfo(python) { UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
            WorkingDirectory = Path.Combine(root, "app") };
        start.ArgumentList.Add("-X"); start.ArgumentList.Add("utf8"); start.ArgumentList.Add(script);
        foreach (var argument in arguments) start.ArgumentList.Add(argument);
        start.Environment["PYTHONUTF8"] = "1";
        start.Environment["PYTHONIOENCODING"] = "utf-8";
        using var process = Process.Start(start) ?? throw new IOException("无法启动工作台连接程序。");
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(40));
        try { await process.WaitForExitAsync(timeout.Token); }
        catch (OperationCanceledException)
        {
            // Do not kill the tree: a workbench may already be starting successfully.
            throw new IOException("连接等待超时。请稍后重新连接并检查日志，不要重复启动或强制结束未知进程。");
        }
        string output = await stdout; string error = await stderr;
        JsonElement value;
        try { value = JsonDocument.Parse(output).RootElement.Clone(); }
        catch (JsonException) { throw new IOException("连接程序未返回有效结果。" + (error.Length > 2000 ? error[..2000] : error)); }
        if (process.ExitCode != 0 || value.GetProperty("status").GetString() == "error")
            throw new IOException(value.TryGetProperty("error", out var detail) ? detail.GetString() : "操作未完成。");
        return value;
    }

    async Task LoadProjects(string? selected = null)
    {
        if (busy) return;
        SetBusy(true);
        try
        {
            var data = await Bridge("list");
            projects.Items.Clear();
            string? preferred = selected ?? data.GetProperty("default_id").GetString();
            foreach (var entry in data.GetProperty("projects").EnumerateArray())
            {
                var project = new Project(entry.GetProperty("id").GetString()!, entry.GetProperty("name").GetString()!, entry.GetProperty("path").GetString()!);
                projects.Items.Add(project);
                if (project.Id == preferred) projects.SelectedItem = project;
            }
            if (projects.SelectedIndex < 0 && projects.Items.Count > 0) projects.SelectedIndex = 0;
            message.Text = "点击启动 / 连接才会启动工作台后台。前端与 Java 后端须在工作台内分别确认启动。\n关闭窗口保留后台；升级前请通过窗口菜单关闭工作台后台，再退出窗口。";
        }
        catch (Exception e) { message.Text = e.Message; }
        finally { SetBusy(false); }
    }

    async Task AddProject()
    {
        if (busy) return;
        using var picker = new FolderBrowserDialog { Description = "选择本地 Git 仓库或已登记 worktree", UseDescriptionForTitle = true };
        if (picker.ShowDialog(this) != DialogResult.OK) return;
        SetBusy(true);
        string? selected = null;
        try { var data = await Bridge("add", "--path", picker.SelectedPath); selected = data.GetProperty("project").GetProperty("id").GetString(); }
        catch (Exception e) { message.Text = e.Message; }
        finally { SetBusy(false); }
        if (selected != null) await LoadProjects(selected);
    }

    async Task EnsureWebView()
    {
        if (web != null) return;
        try { CoreWebView2Environment.GetAvailableBrowserVersionString(); }
        catch (WebView2RuntimeNotFoundException)
        {
            throw new IOException("缺少 Microsoft Edge WebView2 Runtime。请按使用手册安装 Evergreen x64 Runtime 后重试。");
        }
        string data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Branch Desk WebView2");
        var control = new WebView2 { Dock = DockStyle.Fill };
        Controls.Add(control); control.BringToFront(); MainMenuStrip!.BringToFront();
        try
        {
            await control.EnsureCoreWebView2Async(await CoreWebView2Environment.CreateAsync(userDataFolder: data));
            control.CoreWebView2.Settings.AreDefaultContextMenusEnabled = true;
            control.CoreWebView2.Settings.AreDevToolsEnabled = true;
            control.CoreWebView2.Settings.IsStatusBarEnabled = false;
            await control.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync("window.branchDeskDesktop = {};");
            control.CoreWebView2.NavigationStarting += (_, e) =>
            {
                if (IsWorkbench(e.Uri)) return;
                e.Cancel = true;
                if (e.IsUserInitiated) OpenExternal(e.Uri);
            };
            control.CoreWebView2.NewWindowRequested += (_, e) => { e.Handled = true; if (e.IsUserInitiated) OpenExternal(e.Uri); };
            control.CoreWebView2.PermissionRequested += (_, e) => e.State = CoreWebView2PermissionState.Deny;
            control.CoreWebView2.DownloadStarting += (_, e) => e.Cancel = true;
            control.CoreWebView2.ProcessFailed += (_, _) => MessageBox.Show(this, "页面进程异常，请从窗口菜单重新连接。后台任务不会自动重试。", "Branch Desk");
            web = control;
        }
        catch { Controls.Remove(control); control.Dispose(); throw; }
    }

    static bool IsWorkbench(string value) => Uri.TryCreate(value, UriKind.Absolute, out var uri) &&
        uri.Scheme == "http" && uri.Host == "127.0.0.1" && uri.Port > 0 && uri.AbsolutePath == "/" &&
        uri.UserInfo == "" && uri.Query == "" && (uri.Fragment == "" || Regex.IsMatch(uri.Fragment, "^#[-_A-Za-z0-9]{32,128}$"));

    static void OpenExternal(string value)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri)) return;
        bool permitted = uri.Scheme == "https" || (uri.Scheme == "http" && uri.Host == "127.0.0.1") || uri.Scheme == "codex";
        if (!permitted || uri.UserInfo != "") return;
        try { Process.Start(new ProcessStartInfo(value) { UseShellExecute = true }); }
        catch (Exception e) { MessageBox.Show("无法打开链接。请检查相关应用是否已安装。\n" + e.Message, "Branch Desk"); }
    }

    void OpenDocument(string name)
    {
        try { Process.Start(new ProcessStartInfo(Path.Combine(root, "docs", name)) { UseShellExecute = true }); }
        catch (Exception e) { MessageBox.Show(this, e.Message, "无法打开手册"); }
    }

    async Task Connect()
    {
        if (busy || projects.SelectedItem is not Project project) return;
        SetBusy(true); message.Text = "正在检查运行环境并连接…";
        try
        {
            await EnsureWebView(); // Check WebView before starting a background workbench.
            var data = await Bridge("connect", "--id", project.Id, "--start");
            string? url = data.GetProperty("url").GetString();
            if (url == null || !IsWorkbench(url)) throw new IOException("连接地址无效，未打开页面。");
            welcome.Visible = false; web!.Visible = true; web.Source = new Uri(url); loaded = true;
        }
        catch (Exception e) { ShowWelcome(); message.Text = e.Message; }
        finally { SetBusy(false); }
    }

    async Task<bool> CanLeave()
    {
        if (busy) return false;
        if (!loaded || web?.CoreWebView2 == null) return true;
        string? result = null;
        try { result = await web.CoreWebView2.ExecuteScriptAsync("window.branchDeskDesktop?.canLeave?.() ?? null"); }
        catch { }
        if (result == "true") return true;
        if (result == "false")
        {
            MessageBox.Show(this, "页面仍有未保存内容、确认窗口或正在执行的操作。请先完成或取消，再关闭或切换。", "暂不能离开");
            return false;
        }
        return MessageBox.Show(this, "页面状态无法读取，是否离开当前页面？\n尚未保存的页面输入可能丢失。后台任务和开发服务继续运行，重新连接后请先检查状态，不要重复操作。", "页面暂不可用", MessageBoxButtons.OKCancel, MessageBoxIcon.Warning) == DialogResult.OK;
    }

    void ShowWelcome()
    {
        loaded = false;
        if (web != null) { web.Visible = false; web.Dispose(); Controls.Remove(web); web = null; }
        welcome.Visible = true; welcome.BringToFront(); MainMenuStrip!.BringToFront();
    }

    async Task StopBackends()
    {
        if (!await CanLeave()) return;
        if (MessageBox.Show(this, "关闭所有已登记项目的空闲工作台后台？\n正在进行的任务会阻止退出。前端和共用 Java 后端保持运行。", "关闭工作台后台", MessageBoxButtons.OKCancel, MessageBoxIcon.Question) != DialogResult.OK) return;
        SetBusy(true);
        try { var result = await Bridge("shutdown", "--confirmed"); ShowWelcome(); message.Text = result.GetProperty("message").GetString(); }
        catch (Exception e) { MessageBox.Show(this, e.Message, "部分后台未关闭"); }
        finally { SetBusy(false); }
    }

    async void OnWindowClosing(object? sender, FormClosingEventArgs e)
    {
        if (allowClose) return;
        e.Cancel = true;
        if (checkingClose) return;
        checkingClose = true;
        try { if (await CanLeave()) { allowClose = true; Close(); } }
        finally { checkingClose = false; }
    }
}
