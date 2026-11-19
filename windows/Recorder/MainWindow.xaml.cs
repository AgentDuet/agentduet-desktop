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
    private bool settings, ready, narrowDetail, binding, closed, prefilled;
    private MediaPlayerElement? player;
    private int playbackVersion;
    private string? playingId;
    private StackPanel? playingRow;

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
            Header.Padding = new Thickness(16, 0, 148, 0);
        }
        Root.Loaded += async (_, _) =>
        {
            try
            {
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
        Navigation.Visibility = Visible(ready);
        string destination = settings ? "Back to recordings" : "Settings";
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(Navigation, destination);
        ToolTipService.SetToolTip(Navigation, destination);
        NavIcon.Glyph = settings ? "\uE72B" : "\uE713";
        ShowSection();
        if (!agreed && termsVersion == null)
        {
            var terms = await api.Request("terms");
            termsVersion = Text(terms, "version");
            TermsText.Text = Text(terms, "terms");
            PrivacyText.Text = Text(terms, "privacy");
        }
        OwnerName.Text = First(Text(state, "name"), "Account name");
        AccountStatus.Text = signedInStatus();
        string signedInStatus() => Flag(state["oauth"], "signed_in") ? "Signed in with browser" :
            configured ? "Connected with API key " + Text(state, "key_hint") : "";
        OwnerPanel.Visibility = Visible(configured);
        bool signedIn = Flag(state["oauth"], "signed_in");
        SignOutButton.Visibility = Visible(signedIn);
        Manual.Visibility = Browser.Visibility = Visible(!signedIn);
        Identity.Text = signedIn ? Text(state["oauth"], "email") : configured
            ? First(Text(state, "line"), Text(state, "connector_uuid")) : "Connect your AgentDuet phone line.";
        if (!prefilled && agreed)
        {
            Uuid.Text = First(Text(state, "offer_uuid"), Text(state, "connector_uuid"));
            string hint = Text(state, "offer_key");
            if (hint.Length > 0) Key.PlaceholderText = $"Use saved key {hint}";
            prefilled = true;
        }
        Folder.Text = Text(state, "carried_dir");
        Login.IsChecked = Flag(state, "start_at_login");
        FinishButton.Visibility = Visible(!Flag(state, "setup_done"));
        FolderAccess.Text = "Recordings folder: " + Folder.Text;
        StatusDot.Visibility = Connection.Visibility = Visible(configured);
        if (!configured) return;
        if (settings && section == 4 && Version.Text.Length == 0)
            Version.Text = Text(await api.Request("about"), "version");

        var channel = (await api.Request("state"))["channel"];
        Connection.Text = Text(channel, "channel") switch
        {
            "live" => "Connected to your line",
            "connecting" => "Connecting to your line…",
            _ => "Line disconnected"
        };
        StatusDot.Fill = new Microsoft.UI.Xaml.Media.SolidColorBrush(Text(channel, "channel") switch
        {
            "live" => Microsoft.UI.Colors.MediumSeaGreen, "connecting" => Microsoft.UI.Colors.Goldenrod,
            _ => Microsoft.UI.Colors.Gray
        });
        ToolTipService.SetToolTip(Connection, Text(channel, "detail"));
        if (library) await ReadLibrary();
    }

    private static string DisplayDate(string stamp) =>
        DateTime.TryParse(stamp, out var date) ? date.ToString("g") : stamp;

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
                Flag(c, "missed") ? "Missed call" : (int?)c!["files"] > 0 ? "Preparing recording" : "No recording";
            string direction = Flag(c, "outgoing") ? "Outgoing" : "Incoming";
            if (audio.Length > 0) direction += " · " + Duration((double?)c!["seconds"] ?? 0);
            return new Call(Text(c, "call_id"), date, direction, audio, status);
        }).ToArray() ?? [];
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
        Storage.Visibility = Visible(agreed && configured && (!settings || section == 1));
        Permissions.Visibility = Visible(agreed && configured && (!settings || section == 2));
        QuitButton.Visibility = Visible(settings && section == 3);
        About.Visibility = Visible(settings && section == 4);
        if (settings) Heading.Text = ((ListViewItem)Sections.Items[section]).Content.ToString();
        Layout();
    }

    private void ChangeSection(object sender, SelectionChangedEventArgs args)
    {
        if (CompactSections == null || Sections.SelectedIndex < 0) return;
        section = Sections.SelectedIndex;
        CompactSections.SelectedIndex = section;
        ShowSection();
        if (section == 4) _ = Refresh();
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
        var name = await AskName("Edit account name", OwnerName.Text == "Account name" ? "" : OwnerName.Text);
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
        ListColumn.Width = showList && compact ? new GridLength(1, GridUnitType.Star) : new GridLength(showList ? Math.Clamp(Root.ActualWidth * .22, 210, 280) : 0);
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
            Key.Password = "";
            Notice.IsOpen = false;
            if (success != null) Message(success, InfoBarSeverity.Informational);
            await ReadState();
        }
        catch (Exception e) { Message(e.Message, InfoBarSeverity.Error); }
        finally { requests.Release(); if (!closed) Setup.IsEnabled = true; }
    }

    private async void Agree(object sender, RoutedEventArgs e) => await Act("terms", new { version = termsVersion });
    private async void SignIn(object sender, RoutedEventArgs e) => await Act("connector/signin/open", new {}, "Complete sign-in in your browser. You can also use a connector UUID and API key here.");
    private async void SignOut(object sender, RoutedEventArgs e) => await Act("connector/signout", new {});
    private async void Connect(object sender, RoutedEventArgs e) => await Act("setup/connector", new { uuid = Uuid.Text.Trim(), key = Key.Password.Trim() });
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
            await Act("setup/setting", new { field = "recordings", value = folder.Path });
        }
        catch (Exception error) { Message(error.Message, InfoBarSeverity.Error); }
    }
    private async void OpenFolder(object sender, RoutedEventArgs e) => await Act("reveal", new { folder = "recordings" });
    private async void ChangeLogin(object sender, RoutedEventArgs e) => await Act("setup/login-item", new { want = Login.IsChecked == true });
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

    private void DetachPlayer()
    {
        if (playingRow == null) return;
        ((ContentControl)playingRow.FindName("Transport")).Content = null;
        if (playingRow.DataContext is Call call)
            ((Button)playingRow.FindName("PlayButton")).Visibility = call.PlayVisibility;
        playingRow = null;
    }

    private void BindRow(StackPanel row)
    {
        if (row.FindName("PlayButton") is not Button button || row.DataContext is not Call call) return;
        if (playingId == call.Id && player != null)
        {
            if (playingRow != row)
            {
                DetachPlayer();
                playingRow = row;
                ((ContentControl)row.FindName("Transport")).Content = player;
            }
            button.Visibility = Visibility.Collapsed;
        }
        else
        {
            if (playingRow == row) DetachPlayer();
            button.Visibility = call.PlayVisibility;
        }
    }

    private void RowLoaded(object sender, RoutedEventArgs e) => BindRow((StackPanel)sender);
    private void RowChanged(FrameworkElement sender, DataContextChangedEventArgs e) => BindRow((StackPanel)sender);
    private void RowUnloaded(object sender, RoutedEventArgs e)
    {
        if (sender == playingRow) DetachPlayer();
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
        DetachPlayer();
    }

    private async void Play(object sender, RoutedEventArgs args)
    {
        if (sender is not Button { Parent: Grid { Parent: StackPanel row }, DataContext: Call call }) return;
        int version = ++playbackVersion;
        try
        {
            var file = await StorageFile.GetFileFromPathAsync(call.Path);
            if (version != playbackVersion || closed || row.DataContext as Call != call) return;
            var media = AudioPlayer().MediaPlayer;
            playingId = call.Id;
            BindRow(row);
            media.Source = MediaSource.CreateFromStorageFile(file);
            media.Play();
        }
        catch (Exception e) { Message($"Unable to play recording. {e.Message}", InfoBarSeverity.Error); }
    }
}
