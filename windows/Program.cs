using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Reflection;
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
    const string LauncherUrl = "https://branch-desk.local/";
    readonly string root = AppContext.BaseDirectory;
    readonly ToolStripMenuItem home = new("选择项目 / 重新连接");
    readonly ToolStripMenuItem stop = new("关闭所有空闲工作台后台…");
    readonly List<Project> projects = [];
    string? selectedId;
    string message = "";
    bool error, busy, loaded, allowClose, checkingClose;
    WebView2? web;

    public DeskWindow()
    {
        Text = "Branch Desk";
        Width = 1280; Height = 840; MinimumSize = new Size(820, 600);
        StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Microsoft YaHei UI", 10);
        BackColor = Color.FromArgb(247, 249, 250);
        Icon = Icon.ExtractAssociatedIcon(Environment.ProcessPath!);
        var menu = new MenuStrip { BackColor = Color.FromArgb(31, 35, 43), ForeColor = Color.FromArgb(226, 232, 240),
            Padding = new Padding(12, 4, 12, 4), Visible = false, Renderer = new ToolStripProfessionalRenderer(new DeskColors()) };
        var window = new ToolStripMenuItem("项目与窗口");
        window.DropDownItems.AddRange([home, stop]);
        var guide = new ToolStripMenuItem("使用帮助");
        guide.Click += (_, _) => OpenDocument("Windows使用与Codex排障手册.html");
        menu.Items.AddRange([window, guide]);
        MainMenuStrip = menu; Controls.Add(menu);
        home.Click += async (_, _) => { if (await CanLeave()) await ShowWelcome(); };
        stop.Click += async (_, _) => await StopBackends();
        Shown += async (_, _) =>
        {
            try { await EnsureWebView(); await ShowWelcome(); }
            catch (Exception e) { MessageBox.Show(this, e.Message, "无法打开 Branch Desk"); }
        };
        FormClosing += OnWindowClosing;
    }

    void SetBusy(bool value)
    {
        busy = value; home.Enabled = stop.Enabled = !value;
        RenderLauncher();
    }

    void RenderLauncher()
    {
        if (loaded || web?.CoreWebView2 == null) return;
        web.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(new {
            type = "state", busy, message, error, selectedId,
            projects = projects.Select(p => new { id = p.Id, name = p.Name, path = p.Path })
        }));
    }

    async Task ShowWelcome()
    {
        loaded = false; MainMenuStrip!.Visible = false;
        web!.CoreWebView2.Navigate(LauncherUrl);
        await LoadProjects();
    }

    async void LauncherMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        // Only our embedded startup document may invoke native launcher actions.
        if (loaded || e.Source != LauncherUrl) return;
        try
        {
            var data = JsonDocument.Parse(e.WebMessageAsJson).RootElement;
            string? action = data.GetProperty("action").GetString();
            if (action == "ready") { RenderLauncher(); return; }
            if (busy) return;
            switch (action)
            {
                case "select":
                    string? id = data.GetProperty("id").GetString();
                    if (projects.Any(p => p.Id == id)) selectedId = id;
                    RenderLauncher(); break;
                case "connect": await Connect(); break;
                case "add": await AddProject(); break;
                case "help": OpenDocument("Windows使用与Codex排障手册.html"); break;
                case "shutdown": await StopBackends(); break;
            }
        }
        catch (Exception ex) { message = ex.Message; error = true; SetBusy(false); }
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
        message = "正在读取本地项目…"; error = false; SetBusy(true);
        try
        {
            var data = await Bridge("list");
            projects.Clear();
            selectedId = selected ?? selectedId ?? data.GetProperty("default_id").GetString();
            foreach (var entry in data.GetProperty("projects").EnumerateArray())
                projects.Add(new Project(entry.GetProperty("id").GetString()!, entry.GetProperty("name").GetString()!, entry.GetProperty("path").GetString()!));
            if (!projects.Any(p => p.Id == selectedId)) selectedId = projects.FirstOrDefault()?.Id;
            message = "";
        }
        catch (Exception e) { message = e.Message; error = true; }
        finally { SetBusy(false); }
    }

    async Task AddProject()
    {
        if (busy) return;
        using var picker = new FolderBrowserDialog { Description = "选择本地 Git 仓库或 worktree", UseDescriptionForTitle = true };
        if (picker.ShowDialog(this) != DialogResult.OK) return;
        message = "正在添加项目…"; error = false; SetBusy(true);
        string? selected = null;
        try { var data = await Bridge("add", "--path", picker.SelectedPath); selected = data.GetProperty("project").GetProperty("id").GetString(); }
        catch (Exception e) { message = e.Message; error = true; }
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
        control.DefaultBackgroundColor = Color.FromArgb(247, 249, 250);
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
                if (IsWorkbench(e.Uri) || (!loaded && e.Uri == LauncherUrl)) return;
                e.Cancel = true;
                if (e.IsUserInitiated) OpenExternal(e.Uri);
            };
            control.CoreWebView2.NewWindowRequested += (_, e) => { e.Handled = true; if (e.IsUserInitiated) OpenExternal(e.Uri); };
            control.CoreWebView2.PermissionRequested += (_, e) => e.State = CoreWebView2PermissionState.Deny;
            control.CoreWebView2.DownloadStarting += (_, e) => e.Cancel = true;
            control.CoreWebView2.ProcessFailed += (_, _) => MessageBox.Show(this, "页面进程异常，请从窗口菜单重新连接。后台任务不会自动重试。", "Branch Desk");
            control.CoreWebView2.AddWebResourceRequestedFilter(LauncherUrl, CoreWebView2WebResourceContext.Document);
            control.CoreWebView2.WebResourceRequested += (_, e) =>
            {
                if (e.Request.Uri != LauncherUrl) return;
                var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("BranchDesk.launcher.html");
                e.Response = control.CoreWebView2.Environment.CreateWebResourceResponse(stream, 200, "OK", "Content-Type: text/html; charset=utf-8\r\nCache-Control: no-store");
            };
            control.CoreWebView2.WebMessageReceived += LauncherMessage;
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
        if (busy || projects.FirstOrDefault(p => p.Id == selectedId) is not Project project) return;
        message = "正在连接 " + project.Name + "…"; error = false; SetBusy(true);
        try
        {
            await EnsureWebView(); // Check WebView before starting a background workbench.
            var data = await Bridge("connect", "--id", project.Id, "--start");
            string? url = data.GetProperty("url").GetString();
            if (url == null || !IsWorkbench(url)) throw new IOException("连接地址无效，未打开页面。");
            loaded = true; MainMenuStrip!.Visible = true; web!.Source = new Uri(url); MainMenuStrip.BringToFront();
        }
        catch (Exception e) { message = e.Message; error = true; }
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

    async Task StopBackends()
    {
        if (!await CanLeave()) return;
        if (MessageBox.Show(this, "关闭所有已登记项目的空闲工作台后台？\n正在进行的任务会阻止退出。前端和共用 Java 后端保持运行。", "关闭工作台后台", MessageBoxButtons.OKCancel, MessageBoxIcon.Question) != DialogResult.OK) return;
        SetBusy(true);
        try { var result = await Bridge("shutdown", "--confirmed"); SetBusy(false); await ShowWelcome(); message = result.GetProperty("message").GetString() ?? ""; error = false; }
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

internal sealed class DeskColors : ProfessionalColorTable
{
    public override Color MenuItemSelected => Color.FromArgb(56, 65, 78);
    public override Color MenuItemBorder => Color.FromArgb(56, 65, 78);
    public override Color MenuItemPressedGradientBegin => Color.FromArgb(56, 65, 78);
    public override Color MenuItemPressedGradientEnd => Color.FromArgb(56, 65, 78);
}
