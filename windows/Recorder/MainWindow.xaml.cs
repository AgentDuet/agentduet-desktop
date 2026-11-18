using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Windows.Media.Core;
using Windows.Storage;

namespace Recorder;

public sealed partial class MainWindow : Window
{
    public sealed record Person(string Id, string Name, string LastCall)
    {
        public override string ToString() => $"{Name}, {LastCall}";
    }
    public sealed record Call(string Id, string Date, string Description, string Path)
    {
        public override string ToString() => $"{Date}, {Description}";
        public Visibility PlayVisibility => Visible(Path.Length > 0);
    }

    private RecorderApi? api;
    private readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromSeconds(4) };
    private readonly SemaphoreSlim requests = new(1);
    private string? termsVersion;
    private string? selected;
    private Person[] people = [];
    private Call[] calls = [];
    private bool settings, ready, narrowDetail, binding, closed, prefilled;
    private MediaPlayerElement? player;
    private int playbackVersion;
    private string playbackLabel = "";

    public MainWindow()
    {
        InitializeComponent();
        Title = "AgentDuet Recorder";
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
        bool agreed = Flag(state, "terms_agreed");
        bool configured = Flag(state, "connector_configured");
        ready = agreed && configured && Flag(state, "setup_done");
        bool library = ready && !settings;
        if (!library && Playback.Visibility == Visibility.Visible) StopPlayback();
        Heading.Text = library ? "Recordings" : ready ? "Settings" : "Set up Recorder";
        Setup.Visibility = Visible(!library);
        Library.Visibility = Visible(library);
        Navigation.Visibility = Visible(ready);
        Navigation.Content = settings ? "Back to recordings" : "Settings";
        Terms.Visibility = Visible(!agreed);
        Account.Visibility = Visible(agreed && (!configured || settings));
        Storage.Visibility = Visible(agreed && configured);
        if (!agreed && termsVersion == null)
        {
            var terms = await api.Request("terms");
            termsVersion = Text(terms, "version");
            TermsText.Text = Text(terms, "terms");
            PrivacyText.Text = Text(terms, "privacy");
        }
        QuitButton.Visibility = Visible(settings);
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
        if (!library) return;

        var channel = (await api.Request("state"))["channel"];
        Connection.Text = Text(channel, "channel") == "live" ? "Connected to your line" :
            "Line disconnected · " + First(Text(channel, "detail"), Text(channel, "channel"));
        await ReadLibrary();
    }

    private static string DisplayDate(string stamp) =>
        DateTime.TryParse(stamp, out var date) ? date.ToString("g") : stamp;

    private async Task ReadLibrary()
    {
        string? requested = selected;
        var data = await api!.Request("threads?open=" + Uri.EscapeDataString(requested ?? ""));
        if (closed || requested != selected) return;
        var rows = data["people"]!.AsArray();
        var next = rows.Select(p => new Person(Text(p, "who"), First(Text(p, "display"), Text(p, "who")),
            DisplayDate(Text(p, "last")))).ToArray();
        var person = next.FirstOrDefault(p => p.Id == selected) ?? next.FirstOrDefault();
        binding = true;
        try
        {
            if (!people.SequenceEqual(next))
            {
                people = next;
                People.ItemsSource = people;
            }
            People.SelectedItem = people.FirstOrDefault(p => p.Id == person?.Id);
        }
        finally { binding = false; }
        Empty.Visibility = Visible(people.Length == 0);
        PersonName.Text = person?.Name ?? "";
        if (selected != person?.Id)
        {
            ChangePerson(person?.Id);
            if (selected != null) { await ReadLibrary(); return; }
        }
        var items = rows.FirstOrDefault(p => Text(p, "who") == selected)?["calls"]?.AsArray();
        var nextCalls = items?.Select(c =>
        {
            string stamp = First(Text(c, "started"), Text(c, "at"));
            string date = DisplayDate(stamp);
            string audio = Text(c, "audio");
            string status = audio.Length > 0 ? TimeSpan.FromSeconds((double?)c!["seconds"] ?? 0).ToString("g") :
                Flag(c, "missed") ? "Missed call" : (int?)c!["files"] > 0 ? "Preparing recording" : "No recording";
            return new Call(Text(c, "call_id"), date, (Flag(c, "outgoing") ? "Outgoing" : "Incoming") + " · " + status, audio);
        }).ToArray() ?? [];
        if (!calls.SequenceEqual(nextCalls))
        {
            calls = nextCalls;
            Calls.ItemsSource = calls;
        }
        Layout();
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
        bool showDetail = selected != null && (!compact || narrowDetail);
        bool showList = !compact || !showDetail;
        ListPane.Visibility = Visible(showList);
        Details.Visibility = Visible(showDetail);
        Back.Visibility = Visible(compact);
        ListColumn.Width = showList && compact ? new GridLength(1, GridUnitType.Star) : new GridLength(showList ? 260 : 0);
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
            if (folder != null) await Act("pick-folder", new { path = folder.Path });
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

    private MediaPlayerElement AudioPlayer()
    {
        if (player != null) return player;
        var element = new MediaPlayerElement { Height = 64, AreTransportControlsEnabled = true };
        element.TransportControls.IsZoomButtonVisible = false;
        element.SetMediaPlayer(new Windows.Media.Playback.MediaPlayer());
        element.MediaPlayer.MediaOpened += (_, _) => DispatcherQueue.TryEnqueue(() =>
        {
            if (!closed && element.MediaPlayer.Source != null) Playing.Text = playbackLabel;
        });
        element.MediaPlayer.MediaFailed += (_, failure) =>
        {
            var code = failure.ExtendedErrorCode?.HResult;
            DispatcherQueue.TryEnqueue(() =>
            {
                Playing.Text = playbackLabel;
                Message($"Playback failed ({code:X8}). Check your sound output or open the recording from its folder.", InfoBarSeverity.Error);
            });
        };
        PlayerHost.Content = element;
        player = element;
        return element;
    }

    private void StopPlayback()
    {
        playbackVersion++;
        if (player != null)
        {
            player.MediaPlayer.Pause();
            player.MediaPlayer.Source = null;
        }
        Playback.Visibility = Visibility.Collapsed;
    }

    private async void Play(object sender, RoutedEventArgs args)
    {
        if (((FrameworkElement)sender).DataContext is not Call call) return;
        int version = ++playbackVersion;
        try
        {
            var file = await StorageFile.GetFileFromPathAsync(call.Path);
            if (version != playbackVersion || closed) return;
            playbackLabel = $"{PersonName.Text} · {call.Date}";
            Playing.Text = "Opening recording…";
            var media = AudioPlayer().MediaPlayer;
            media.Source = MediaSource.CreateFromStorageFile(file);
            Playback.Visibility = Visibility.Visible;
            media.Play();
        }
        catch (Exception e) { Message($"Unable to play recording. {e.Message}", InfoBarSeverity.Error); }
    }
}
