using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Recorder;

public sealed partial class ConnectionDialog : ContentDialog
{
    private readonly RecorderApi api;
    private bool manual, saving, open;
    private DateTime? waitingSince;

    internal ConnectionDialog(RecorderApi api, string connector, bool manual)
    {
        this.api = api;
        InitializeComponent();
        Uuid.Text = connector;
        SetPage(manual);
        Loaded += (_, _) => DialogBody.Width = Math.Clamp(XamlRoot.Size.Width - 96, 180, 410);
        Opened += (_, _) => open = true;
        Closed += (_, _) => { open = false; Key.Password = ""; };
        Closing += (_, args) => args.Cancel = saving;
    }

    private void SetPage(bool useKey)
    {
        manual = useKey;
        waitingSince = null;
        BrowserButton.IsEnabled = true;
        ManualPage.Visibility = manual ? Visibility.Visible : Visibility.Collapsed;
        BrowserPage.Visibility = manual ? Visibility.Collapsed : Visibility.Visible;
        // Keep Back separate from submission in the connector footer.
        CloseButtonText = manual ? "" : "Cancel";
        ManualFooter.Visibility = ManualPage.Visibility;
        Feedback.Visibility = Visibility.Collapsed;
        Validate();
    }

    private void Manual(object sender, RoutedEventArgs args) { SetPage(true); Uuid.Focus(FocusState.Programmatic); }
    private void GoBack(object sender, RoutedEventArgs args) { Key.Password = ""; SetPage(false); BrowserButton.Focus(FocusState.Programmatic); }
    private void Cancel(object sender, RoutedEventArgs args) => Hide();

    private void Say(string message)
    {
        Feedback.Text = message;
        Feedback.Visibility = Visibility.Visible;
    }

    private async void OpenBrowser(object sender, RoutedEventArgs args)
    {
        BrowserButton.IsEnabled = false;
        Say("Waiting for browser…");
        try
        {
            await api.Request("connector/signin/open", new {});
            if (open && !manual) waitingSince = DateTime.UtcNow;
        }
        catch (Exception) { BrowserFailed(); }
    }

    private void BrowserFailed()
    {
        waitingSince = null;
        BrowserButton.IsEnabled = true;
        Say("Sign-in failed. Try again.");
    }

    internal void Observe(JsonNode state)
    {
        if (!open || manual || waitingSince is not { } since) return;
        if ((bool?)state["oauth"]?["signed_in"] == true) Hide();
        else if ((bool?)state["oauth"]?["failed"] == true || DateTime.UtcNow - since >= TimeSpan.FromMinutes(5)) BrowserFailed();
    }

    private void Validate()
    {
        if (ConnectButton == null || Key == null) return;
        ConnectButton.IsEnabled = !saving && Uuid.Text.Trim().Length > 0 && Key.Password.Trim().Length > 0;
    }

    private void UuidChanged(object sender, TextChangedEventArgs args) => Validate();
    private void KeyChanged(object sender, RoutedEventArgs args) => Validate();

    private async void Save(object sender, RoutedEventArgs args)
    {
        if (!Guid.TryParseExact(Uuid.Text.Trim(), "D", out var id))
        {
            Say("Enter a UUID in the format 00000000-0000-0000-0000-000000000000.");
            Uuid.Focus(FocusState.Programmatic);
            return;
        }
        saving = true;
        Uuid.IsEnabled = Key.IsEnabled = BackButton.IsEnabled = CancelButton.IsEnabled = false;
        Validate();
        try
        {
            await api.Request("setup/connector", new { uuid = id.ToString(), key = Key.Password.Trim(), use_key = true });
            saving = false;
            Hide();
        }
        catch (Exception) { Say("Couldn’t connect. Check the UUID and API key, then try again."); }
        finally
        {
            saving = false;
            Uuid.IsEnabled = Key.IsEnabled = BackButton.IsEnabled = CancelButton.IsEnabled = true;
            Validate();
        }
    }
}
