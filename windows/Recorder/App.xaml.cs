using Microsoft.UI.Xaml;

namespace Recorder;

public partial class App : Application
{
    private Window? window;
    public App()
    {
        UnhandledException += (_, error) => ReportStartup(error.Exception);
        try { InitializeComponent(); }
        catch (Exception error) { ReportStartup(error); throw; }
    }

    // Capture failures constructing the native window before there is a surface for errors.
    internal static void ReportStartup(Exception error)
    {
        System.Diagnostics.Trace.WriteLine(error);
        Console.Error.WriteLine(error);
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        try
        {
            window = new MainWindow();
            window.Activate();
        }
        catch (Exception error) { ReportStartup(error); throw; }
    }
}
