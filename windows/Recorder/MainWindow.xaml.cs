using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Windows.Media.Core;
using Windows.Storage;

namespace Recorder;

public sealed partial class MainWindow : Window
{
    public sealed record Person(string Id, string Name, string LastCall, int Count)
    {
        public string CountText => Count == 1 ? "1 call" : $"{Count} calls";
        public string Summary => $"{CountText} · {LastCall}";
        public string Initials => Name == Id ? "" : string.Concat(Name.Split(' ', StringSplitOptions.RemoveEmptyEntries)
            .Take(2).Select(word => System.Globalization.StringInfo.GetNextTextElement(word))).ToUpperInvariant();
        public override string ToString() => $"{Name}, {Summary}";
    }
    public sealed record Call(string Id, string Date, string Description, string Path, string Status)
    {
        public override string ToString() => $"{Date}, {Description}";
        public Visibility StatusVisibility => Visible(Status.Length > 0);
        public Visibility PlayVisibility => Visible(Path.Length > 0);
    }

    private RecorderApi? api;
    private readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromSeconds(4) };
    private readonly SemaphoreSlim requests = new(1);
    private string? termsVersion;
    private string? selected;
    private Person[] people = [];
    private Person[] filtered = [];
    private bool agreed, configured;
    private int section;
    private Call[] calls = [];
    private bool settings, ready, narrowDetail, binding, closed, loadingLogin;
    private MediaPlayerElement? player;
    private int playbackVersion;
    private string? playingId;
    private Flyout? playbackFlyout;
    private string connectorId = "";
    private ConnectionDialog? connectionDialog;
    private bool checkingUpdates;
    private (string Path, DateTime Checked, string Warning)? folderCheck;

    public MainWindow()
    {
        InitializeComponent();
        Title = "AgentDuet Recorder";
        AppWindow.SetIcon(System.IO.Path.Combine(AppContext.BaseDirectory, "Recorder.ico"));
        if (Microsoft.UI.Windowing.AppWindowTitleBar.IsCustomizationSupported())
        {
            ExtendsContentIntoTitleBar = true;
            SetTitleBar(DragArea);
            AppWindow.TitleBar.ButtonForegroundColor = Microsoft.UI.Colors.White;
            AppWindow.TitleBar.ButtonBackgroundColor = Microsoft.UI.Colors.Transparent;
            AppWindow.TitleBar.ButtonInactiveBackgroundColor = Microsoft.UI.Colors.Transparent;
        }
        Root.Loaded += async (_, _) =>
        {
            try
            {
                CaptionInsets();
                Root.XamlRoot.Changed += (_, _) => CaptionInsets();
                api = new RecorderApi(Console.In.ReadLine());
                await Refresh();
                timer.Start();
            }
            catch (Exception e) { Message(e.Message, InfoBarSeverity.Error); }
        };
        timer.Tick += async (_, _) => await Refresh();
        Closed += (_, _) =>
        {
            closed = true;
            timer.Stop();
            StopPlayback();
            api?.Dispose();
        };
    }

    private static Visibility Visible(bool value) => value ? Visibility.Visible : Visibility.Collapsed;
    private static string Text(JsonNode? obj, string key) => (string?)obj?[key] ?? "";
    private static bool Flag(JsonNode? obj, string key) => (bool?)obj?[key] == true;
    private static string First(string value, string fallback) => value.Length == 0 ? fallback : value;

    private void Message(string text, InfoBarSeverity severity)
    {
        if (closed) return;
        Notice.Message = text;
        Notice.Severity = severity;
        Notice.IsOpen = true;
    }

    private async Task Refresh()
    {
        if (api == null || closed || !await requests.WaitAsync(0)) return;
        try { await ReadState(); }
        catch (Exception e) { Message(e.Message, InfoBarSeverity.Error); }
        finally { requests.Release(); }
    }

    private async Task ReadState()
    {
        var state = await api!.Request("setup/current");
        if (closed) return;
        agreed = Flag(state, "terms_agreed");
        configured = Flag(state, "connector_configured");
        ready = agreed && configured && Flag(state, "setup_done");
        bool library = ready && !settings;
        if (!library && playingId != null) StopPlayback();
        Heading.Text = library ? "Recordings" : ready ? "Settings" : "Set up Recorder";
        Setup.Visibility = Visible(!library);
        Library.Visibility = Visible(library);
        Navigation.Visibility = Visible(agreed && !settings);
        SettingsBack.Visibility = Visible(settings);
        ShowSection();
        if (!agreed && termsVersion == null)
        {
            var terms = await api.Request("terms");
            termsVersion = Text(terms, "version");
            TermsText.Text = Text(terms, "terms");
            PrivacyText.Text = Text(terms, "privacy");
        }
        OwnerName.Text = First(Text(state, "name"), First(Text(state, "os_name"), "Not set"));
        Number.Text = First(Text(state, "line"), First(Text(state, "phone"), "Unavailable"));
        NumberRow.Visibility = KeyRow.Visibility = Visible(configured);
        SignInRow.Visibility = Visible(!configured);
        KeyHint.Text = First(Text(state, "key_hint"), "No saved key");
        AccountStatus.Text = Connection.Text = configured ? "Connected" : "Not signed in";
        StatusDot.Visibility = Visibility.Visible;
        AccountDot.Fill = StatusDot.Fill = (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources[
            configured ? "SystemFillColorSuccessBrush" : "TextFillColorSecondaryBrush"];
        connectorId = First(Text(state, "connector_uuid"), First(Text(state["oauth"], "connector"), Text(state, "offer_uuid")));
        connectionDialog?.Observe(state);
        Folder.Text = Text(state, "carried_dir");
        ToolTipService.SetToolTip(Folder, Folder.Text);
        loadingLogin = true;
        Login.IsOn = Flag(state, "start_at_login");
        loadingLogin = false;
        FinishButton.Visibility = Visible(!settings && agreed && configured && !Flag(state, "setup_done"));
        if (Storage.Visibility == Visibility.Visible || Permissions.Visibility == Visibility.Visible)
            await CheckFolder();
        if (settings && section == 3 && !checkingUpdates)
        {
            var about = await api.Request("about");
            Version.Text = Text(about, "version");
            if (!checkingUpdates) RenderUpdate(about["update"]);
        }
        if (library) await ReadLibrary();
    }

    private static string DisplayDate(string stamp)
    {
        if (!DateTimeOffset.TryParse(stamp, out var parsed)) return stamp;
        var local = parsed.LocalDateTime;
        var day = local.Date == DateTime.Today ? "Today" :
            local.Date == DateTime.Today.AddDays(-1) ? "Yesterday" : local.ToString("d");
        return $"{day} at {local:t}";
    }

    private static string Duration(double seconds) =>
        TimeSpan.FromSeconds(seconds).ToString(seconds >= 3600 ? @"h\:mm\:ss" : @"m\:ss");

    private async Task ReadLibrary()
    {
        string? requested = selected;
        var data = await api!.Request("threads?open=" + Uri.EscapeDataString(requested ?? ""));
        if (closed || requested != selected) return;
        var rows = data["people"]!.AsArray();
        var next = rows.Select(p => new Person(Text(p, "who"), First(Text(p, "display"), Text(p, "who")),
            DisplayDate(Text(p, "last")), (int?)p!["call_count"] ?? p["calls"]!.AsArray().Count)).ToArray();
        people = next;
        ApplySearch();
        if (requested != selected && selected != null) { await ReadLibrary(); return; }
        var items = rows.FirstOrDefault(p => Text(p, "who") == selected)?["calls"]?.AsArray();
        var nextCalls = items?.Select(c =>
        {
            string stamp = First(Text(c, "started"), Text(c, "at"));
            string date = DisplayDate(stamp);
            string audio = Text(c, "audio");
            string status = audio.Length > 0 ? "" :
                Flag(c, "missed") ? "Missed call." : (int?)c!["files"] > 0 ? "Preparing recording" : "No recording";
            string direction = Flag(c, "outgoing") ? "Outgoing" : "Incoming";
            direction += " · Voice call";
            if (audio.Length > 0) direction += " · " + Duration((double?)c!["seconds"] ?? 0);
            return new Call(Text(c, "call_id"), date, direction, audio, status);
        }).ToArray() ?? [];
        NoCalls.Visibility = Visible(selected != null && nextCalls.Length == 0);
        if (!calls.SequenceEqual(nextCalls))
        {
            if (playingId != null && !nextCalls.Any(c => c.Id == playingId)) StopPlayback();
            calls = nextCalls;
            Calls.ItemsSource = calls;
        }
        Layout();
    }

    private void ApplySearch()
    {
        string query = Search.Text.Trim();
        var matches = people.Where(p => p.Name.Contains(query, StringComparison.CurrentCultureIgnoreCase)
            || p.Id.Contains(query, StringComparison.OrdinalIgnoreCase)).ToArray();
        binding = true;
        try
        {
            if (!filtered.SequenceEqual(matches))
            {
                filtered = matches;
                People.ItemsSource = filtered;
            }
            var person = filtered.FirstOrDefault(p => p.Id == selected) ?? filtered.FirstOrDefault();
            People.SelectedItem = person;
            ChangePerson(person?.Id);
            PersonName.Text = person?.Name ?? "";
            Avatar.Initials = person?.Initials ?? "";
            PersonInfo.Text = person == null ? "" : person.Name == person.Id ? person.CountText : $"{person.Id} · {person.CountText}";
        }
        finally { binding = false; }
        Empty.Text = people.Length == 0 ? "Your recorded calls will appear here." : "No matching people";
        Empty.Visibility = Visible(filtered.Length == 0);
        Layout();
    }

    private async void SearchChanged(object sender, TextChangedEventArgs e)
    {
        if (!ready) return;
        var before = selected;
        ApplySearch();
        if (before != selected && selected != null) await Refresh();
    }

    private void ShowSection()
    {
        if (About == null) return;
        Terms.Visibility = Visible(!agreed);
        Account.Visibility = Visible(agreed && (settings ? section == 0 : !configured));
        Storage.Visibility = Visible(agreed && (settings ? section == 1 : configured));
        Permissions.Visibility = Visible(agreed && (settings ? section == 2 : configured));
        About.Visibility = Visible(settings && section == 3);
        if (settings) Heading.Text = ((ListViewItem)Sections.Items[section]).Tag.ToString();
        Layout();
    }

    private void ChangeSection(object sender, SelectionChangedEventArgs args)
    {
        if (CompactSections == null || Sections.SelectedIndex < 0) return;
        section = Sections.SelectedIndex;
        folderCheck = null;
        CompactSections.SelectedIndex = section;
        ShowSection();
        _ = Refresh();
    }

    private void ChangeCompactSection(object sender, SelectionChangedEventArgs args)
    {
        if (CompactSections?.SelectedIndex >= 0) Sections.SelectedIndex = CompactSections.SelectedIndex;
    }

    private async Task<string?> AskName(string title, string value)
    {
        var input = new TextBox { Header = "Name", Text = value };
        var dialog = new ContentDialog { XamlRoot = Root.XamlRoot, Title = title, Content = input,
            PrimaryButtonText = "Save", CloseButtonText = "Cancel", DefaultButton = ContentDialogButton.Primary };
        return await dialog.ShowAsync() == ContentDialogResult.Primary ? input.Text.Trim() : null;
    }

    private async void EditPerson(object sender, RoutedEventArgs args)
    {
        if (People.SelectedItem is not Person person) return;
        var name = await AskName("Edit person name", person.Name == person.Id ? "" : person.Name);
        if (name != null) await Act("name", new { who = person.Id, name });
    }

    private async void EditOwner(object sender, RoutedEventArgs args)
    {
        var name = await AskName("Edit account name", OwnerName.Text == "Not set" ? "" : OwnerName.Text);
        if (name != null) await Act("setup/setting", new { field = "name", value = name });
    }

    private async Task ShowLegal(string title, string key)
    {
        try
        {
            var terms = await api!.Request("terms");
            var body = new TextBlock { Text = Text(terms, key), TextWrapping = TextWrapping.Wrap, IsTextSelectionEnabled = true };
            await new ContentDialog { XamlRoot = Root.XamlRoot, Title = title,
                Content = new ScrollViewer { Content = body }, CloseButtonText = "Close" }.ShowAsync();
        }
        catch (Exception e) { Message(e.Message, InfoBarSeverity.Error); }
    }
    private async void ShowTerms(object sender, RoutedEventArgs e) => await ShowLegal("Terms of Use", "terms");
    private async void ShowPrivacy(object sender, RoutedEventArgs e) => await ShowLegal("Privacy Policy", "privacy");
    private async void ExportLogs(object sender, RoutedEventArgs args)
    {
        try
        {
            var picker = new Windows.Storage.Pickers.FileSavePicker { SuggestedFileName = "AgentDuet Recorder logs" };
            picker.FileTypeChoices.Add("ZIP archive", new List<string> { ".zip" });
            WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
            var file = await picker.PickSaveFileAsync();
            if (file != null) await FileIO.WriteBytesAsync(file, await api!.DownloadLogs());
        }
        catch (Exception e) { Message(e.Message, InfoBarSeverity.Error); }
    }

    private void ChangePerson(string? id)
    {
        if (selected == id) return;
        selected = id;
        calls = [];
        Calls.ItemsSource = calls;
        StopPlayback();
    }

    private void OnResize(object sender, SizeChangedEventArgs args) => Layout();
    private void Layout()
    {
        if (ListPane == null) return;
        bool compact = Root.ActualWidth < 700;
        Sections.Visibility = Visible(settings && !compact);
        CompactSections.Visibility = Visible(settings && compact);
        if (CompactSections.SelectedIndex != section) CompactSections.SelectedIndex = section;
        PersonName.MaxWidth = Math.Max(80, Root.ActualWidth - (compact ? 100 : 370));
        bool showDetail = selected != null && (!compact || narrowDetail);
        bool showList = !compact || !showDetail;
        ListPane.Visibility = Visible(showList);
        Details.Visibility = Visible(showDetail);
        Back.Visibility = Visible(compact);
        ListColumn.Width = showList && compact ? new GridLength(1, GridUnitType.Star) : new GridLength(showList ? Math.Clamp(Root.ActualWidth * .29, 225, 300) : 0);
        DetailColumn.Width = showDetail ? new GridLength(1, GridUnitType.Star) : new GridLength(0);
        Panes.ColumnSpacing = showDetail && showList ? 16 : 0;
    }

    private async void SelectPerson(object sender, SelectionChangedEventArgs args)
    {
        if (binding || People.SelectedItem is not Person p) return;
        ChangePerson(p.Id);
        PersonName.Text = p.Name;
        narrowDetail = true;
        Layout();
        await Refresh();
    }

    private void OpenPerson(object sender, ItemClickEventArgs args) { narrowDetail = true; Layout(); }
    private void GoBack(object sender, RoutedEventArgs args) { narrowDetail = false; Layout(); People.Focus(FocusState.Programmatic); }
    private async void Navigate(object sender, RoutedEventArgs args) { settings = !settings; await Refresh(); }

    private async Task Act(string path, object body, string? success = null)
    {
        if (api == null || closed) return;
        Setup.IsEnabled = false;
        await requests.WaitAsync();
        try
        {
            if (closed) return;
            await api.Request(path, body);
            Notice.IsOpen = false;
            if (success != null) Message(success, InfoBarSeverity.Informational);
            await ReadState();
        }
        catch (Exception e) { Message(e.Message, InfoBarSeverity.Error); }
        finally { requests.Release(); if (!closed) Setup.IsEnabled = true; }
    }

    private async void Agree(object sender, RoutedEventArgs e) => await Act("terms", new { version = termsVersion });
    private void CaptionInsets()
    {
        if (!ExtendsContentIntoTitleBar || Root.XamlRoot == null) return;
        var bar = AppWindow.TitleBar;
        double scale = Root.XamlRoot.RasterizationScale;
        Header.Padding = new Thickness(10 + bar.LeftInset / scale, 0, 8 + bar.RightInset / scale, 0);
    }

    private void RenderUpdate(JsonNode? update)
    {
        string checkedAt = Text(update, "checked");
        string message = checkedAt.Length == 0 ? "No check yet." :
            !Flag(update, "reachable") ? "Couldn’t check for updates." :
            Flag(update, "newer") ? $"Version {Text(update, "version")} is available." :
            Text(update, "version").Length == 0 ? "No published Windows release found." : "You are on the latest version.";
        UpdateStatus.Text = checkedAt.Length == 0 ? message : $"{message} Last checked {DisplayDate(checkedAt)}.";
    }

    private async void CheckUpdates(object sender, RoutedEventArgs args)
    {
        if (checkingUpdates || api == null) return;
        checkingUpdates = true;
        UpdateButton.IsEnabled = false;
        UpdateStatus.Text = "Checking for updates…";
        try { RenderUpdate((await api.Request("about/check", new {}))["update"]); }
        catch (Exception) { UpdateStatus.Text = "Couldn’t check for updates. Try again."; }
        finally { checkingUpdates = false; UpdateButton.IsEnabled = true; }
    }

    private static string FolderProblem(string path)
    {
        try
        {
            using var probe = new FileStream(System.IO.Path.Combine(path, System.IO.Path.GetRandomFileName()),
                FileMode.CreateNew, FileAccess.Write, FileShare.None, 4096, FileOptions.DeleteOnClose);
            return "";
        }
        catch (DirectoryNotFoundException) { return "The recordings folder is missing. Choose a folder to continue."; }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or ArgumentException)
        { return "Recorder can’t write to this folder. Choose a writable folder."; }
    }

    private async Task CheckFolder()
    {
        var path = Folder.Text;
        if (folderCheck is not { } cached || cached.Path != path || DateTime.UtcNow - cached.Checked > TimeSpan.FromSeconds(30))
            folderCheck = (path, DateTime.UtcNow, await Task.Run(() => FolderProblem(path)));
        var warning = folderCheck.Value.Warning;
        FolderWarning.Text = warning;
        FolderWarning.Visibility = FixFolder.Visibility = Visible(warning.Length > 0);
        FolderAccess.Text = warning.Length == 0 ? "Allowed" : "Not allowed";
        FolderAccess.Foreground = (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources[
            warning.Length == 0 ? "SystemFillColorSuccessBrush" : "SystemFillColorCriticalBrush"];
    }

    private async Task ShowConnection(bool manual)
    {
        if (api == null || connectionDialog != null) return;
        try
        {
            connectionDialog = new ConnectionDialog(api, connectorId, manual) { XamlRoot = Root.XamlRoot };
            await connectionDialog.ShowAsync();
            await Refresh();
        }
        finally { connectionDialog = null; }
    }
    private async void SignIn(object sender, RoutedEventArgs args) => await ShowConnection(false);
    private async void ChangeKey(object sender, RoutedEventArgs args) => await ShowConnection(true);
    private async void ChooseFolder(object sender, RoutedEventArgs e)
    {
        try
        {
            var picker = new Windows.Storage.Pickers.FolderPicker();
            WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
            picker.FileTypeFilter.Add("*");
            var folder = await picker.PickSingleFolderAsync();
            if (folder == null) return;
            if (!System.IO.Path.IsPathFullyQualified(folder.Path))
            {
                Message("Choose a folder with a local or network path.", InfoBarSeverity.Error);
                return;
            }
            // Check the folder selected by Windows before changing the saved location.
            // CreateNew plus DeleteOnClose protects existing files and cleans up on exit.
            var probe = System.IO.Path.Combine(folder.Path, System.IO.Path.GetRandomFileName());
            using (new FileStream(probe, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                                  4096, FileOptions.DeleteOnClose)) { }
            folderCheck = null;
            await Act("setup/setting", new { field = "recordings", value = folder.Path });
        }
        catch (Exception error) { Message(error.Message, InfoBarSeverity.Error); }
    }
    private async void OpenFolder(object sender, RoutedEventArgs e) => await Act("reveal", new { folder = "recordings" });
    private async void ChangeLogin(object sender, RoutedEventArgs e) { if (!loadingLogin && api != null) await Act("setup/login-item", new { want = Login.IsOn }); }
    private async void Finish(object sender, RoutedEventArgs e) => await Act("setup/finish", new {});

    private async void Quit(object sender, RoutedEventArgs args)
    {
        var dialog = new ContentDialog
        {
            XamlRoot = Root.XamlRoot, Title = "Quit Recorder?",
            Content = "Quitting stops recording, including any call in progress. Closing the window lets Recorder continue in the background.",
            PrimaryButtonText = "Quit Recorder", CloseButtonText = "Cancel", DefaultButton = ContentDialogButton.Close
        };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
        try { await api!.Request("quit", new {}); Close(); }
        catch (Exception e) { Message(e.Message, InfoBarSeverity.Error); }
    }

    private MediaPlayerElement AudioPlayer()
    {
        if (player != null) return player;
        var element = new MediaPlayerElement { Height = 104, AreTransportControlsEnabled = true };
        element.TransportControls.IsCompact = false;
        element.TransportControls.IsZoomButtonVisible = false;
        element.TransportControls.IsSeekBarVisible = true;
        element.TransportControls.ShowAndHideAutomatically = false;
        element.SetMediaPlayer(new Windows.Media.Playback.MediaPlayer());
        element.MediaPlayer.MediaFailed += (_, failure) =>
        {
            var code = failure.ExtendedErrorCode?.HResult;
            DispatcherQueue.TryEnqueue(() =>
            {
                if (closed) return;
                StopPlayback();
                Message($"Playback failed ({code:X8}). Check your sound output or open the recording from its folder.", InfoBarSeverity.Error);
            });
        };
        player = element;
        return element;
    }

    private void StopPlayback()
    {
        playbackVersion++;
        playingId = null;
        if (player != null)
        {
            player.MediaPlayer.Pause();
            player.MediaPlayer.Source = null;
        }
        var flyout = playbackFlyout;
        playbackFlyout = null;
        if (flyout != null) { flyout.Content = null; flyout.Hide(); }
    }

    private async void Play(object sender, RoutedEventArgs args)
    {
        if (sender is not Button { DataContext: Call call } button) return;
        StopPlayback();
        int version = playbackVersion;
        try
        {
            var file = await StorageFile.GetFileFromPathAsync(call.Path);
            if (version != playbackVersion || closed || button.DataContext as Call != call) return;
            var media = AudioPlayer().MediaPlayer;
            playingId = call.Id;
            player!.Width = Math.Clamp(Root.ActualWidth - 88, 180, 400);
            var flyout = new Flyout { Content = player, Placement = Microsoft.UI.Xaml.Controls.Primitives.FlyoutPlacementMode.Bottom };
            playbackFlyout = flyout;
            flyout.Closed += (_, _) =>
            {
                flyout.Content = null;
                if (playbackFlyout == flyout) StopPlayback();
            };
            flyout.ShowAt(button);
            media.Source = MediaSource.CreateFromStorageFile(file);
            media.Play();
        }
        catch (Exception e) { Message($"Unable to play recording. {e.Message}", InfoBarSeverity.Error); }
    }
}
