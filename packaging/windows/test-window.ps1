# Launch via the installed entry point against the running fixture daemon. Checking
# accessibility elements exercises WinUI, its packaged runtime, and authenticated setup.
param([Parameter(Mandatory)][string]$InstallDir, [switch]$Library)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$launcher = Start-Process "$InstallDir/AgentDuet Recorder.exe" -ArgumentList "run --no-channel" -PassThru
try {
    $desktop = [System.Windows.Automation.AutomationElement]::RootElement
    $name = [System.Windows.Automation.AutomationElement]::NameProperty
    $title = [System.Windows.Automation.PropertyCondition]::new($name, 'AgentDuet Recorder')
    $scope = [System.Windows.Automation.TreeScope]::Descendants
    function Find-Control([string]$label) {
        $condition = [System.Windows.Automation.PropertyCondition]::new($name, $label)
        for ($i = 0; $i -lt 30; $i++) {
            $control = $window.FindFirst($scope, $condition)
            if ($control) { return $control }
            Start-Sleep -Milliseconds 500
        }
        throw "Missing native control: $label"
    }
    function Invoke-Control([string]$label) {
        (Find-Control $label).GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    }
    function Save-Window([string]$screen) {
        Add-Type -AssemblyName System.Drawing
        $rect = $window.Current.BoundingRectangle
        $bmp = [Drawing.Bitmap]::new([int]$rect.Width, [int]$rect.Height)
        $graphics = [Drawing.Graphics]::FromImage($bmp)
        try {
            $graphics.CopyFromScreen([int]$rect.Left, [int]$rect.Top, 0, 0, $bmp.Size)
            $output = New-Item -ItemType Directory -Force -Path "$env:RUNNER_TEMP/Recorder screens"
            $bmp.Save("$output/$screen.png", [Drawing.Imaging.ImageFormat]::Png)
        } finally { $graphics.Dispose(); $bmp.Dispose() }
    }
    for ($i = 0; $i -lt 90; $i++) {
        if ($launcher.HasExited) { throw 'Installed launcher exited before setup appeared' }
        $window = $desktop.FindFirst([System.Windows.Automation.TreeScope]::Children, $title)
        if ($window) { break }
        Start-Sleep -Seconds 1
    }
    if (!$window) { throw 'Native window did not appear' }
    if ($Library) {
        Invoke-Control 'Finish setup'
        Invoke-Control 'Play recording'
        $playing = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'Playing')
        for ($i = 0; $i -lt 30; $i++) {
            $caption = $window.FindFirst($scope, $playing)
            if ($caption -and $caption.Current.Name -match '12025550123') { break }
            Start-Sleep -Milliseconds 500
        }
        if (!$caption -or $caption.Current.Name -notmatch '12025550123') { throw 'Recording did not load' }
        Save-Window 'recordings'
        Invoke-Control 'Settings'
        $null = Find-Control 'Start at login'
        Save-Window 'settings'
        Invoke-Control 'Back to recordings'
        $null = Find-Control 'Play recording'
    } else {
        Invoke-Control 'I agree'
        $null = Find-Control 'Sign in with browser'
        $null = Find-Control 'Use a connector UUID and API key'
        Save-Window 'sign-in'
    }
} finally {
    if (!$launcher.HasExited) { taskkill /pid $launcher.Id /t /f | Out-Null }
}
