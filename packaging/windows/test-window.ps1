# Launch via the installed entry point against the running fixture daemon. Checking
# accessibility elements exercises WinUI, its packaged runtime, and authenticated setup.
param([Parameter(Mandatory)][string]$InstallDir, [switch]$Library)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
$reopened = $null
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
        Start-Sleep -Milliseconds 500
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
        $search = (Find-Control 'Search people').GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $search.SetValue('unmatched fixture')
        $null = Find-Control 'No matching people'
        $search.SetValue('alex')
        $null = Find-Control 'Alex Morgan'
        $search.SetValue('')
        $null = Find-Control 'Missed call.'
        Save-Window 'people'
        Invoke-Control 'Play recording'
        $null = Find-Control 'Pause'
        $null = Find-Control 'Seek'
        Save-Window 'recordings'
        [System.Windows.Forms.SendKeys]::SendWait('{ESC}')
        Invoke-Control 'Settings'
        $null = Find-Control 'Edit account name'
        Save-Window 'account'
        (Find-Control 'Recording').GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
        $null = Find-Control 'Choose folder'
        Save-Window 'recording-settings'
        (Find-Control 'About').GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
        $null = Find-Control 'Check now'
        Save-Window 'about'
        (Find-Control 'Permissions').GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
        $null = Find-Control 'Start when I log in'
        Save-Window 'settings'
        $switch = Find-Control 'Start when I log in'
        $toggle = $switch.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
        $startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'agentduet-recorder.cmd'
        $toggle.Toggle()
        for ($i = 0; $i -lt 30 -and !(Test-Path $startup); $i++) { Start-Sleep -Milliseconds 200 }
        if (!(Test-Path $startup)) { throw 'Native settings did not register start at login' }
        $toggle.Toggle()
        for ($i = 0; $i -lt 30 -and (Test-Path $startup); $i++) { Start-Sleep -Milliseconds 200 }
        if (Test-Path $startup) { throw 'Native settings did not remove start at login' }
        Invoke-Control 'Back to recordings'
        $null = Find-Control 'Play recording'
        $window.GetCurrentPattern([System.Windows.Automation.TransformPattern]::Pattern).Resize(550, 480)
        $kind = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::ListItem)
        $person = $window.FindFirst($scope, $kind)
        $person.SetFocus()
        [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
        $null = Find-Control 'Back to people'
        $null = Find-Control 'Play recording'
        Save-Window 'narrow-call'
        Invoke-Control 'Back to people'

        $engine = Get-Content "$env:AGENTDUET_HOME/run/secretary.pid" -Raw
        $window.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close()
        for ($i = 0; $i -lt 30; $i++) {
            if (!$desktop.FindFirst([System.Windows.Automation.TreeScope]::Children, $title)) { break }
            Start-Sleep -Milliseconds 200
        }
        $reopened = Start-Process "$InstallDir/AgentDuet Recorder.exe" -PassThru
        for ($i = 0; $i -lt 60; $i++) {
            $window = $desktop.FindFirst([System.Windows.Automation.TreeScope]::Children, $title)
            if ($window) { break }
            Start-Sleep -Milliseconds 500
        }
        $null = Find-Control 'Play recording'
        if ((Get-Content "$env:AGENTDUET_HOME/run/secretary.pid" -Raw) -ne $engine) {
            throw 'Reopening the library restarted the recording engine'
        }
    } else {
        Invoke-Control 'I agree'
        Invoke-Control 'Sign in to account'
        $null = Find-Control 'Sign in to AgentDuet'
        Save-Window 'first-run-sign-in'
        Invoke-Control 'Cancel'
        Invoke-Control 'Settings'
        $null = Find-Control 'Not signed in'
        Save-Window 'account-signed-out'
        Invoke-Control 'Sign in to account'
        $null = Find-Control 'Sign in'
        Save-Window 'sign-in'
        Invoke-Control 'Use a connector and API key'
        $submit = Find-Control 'Connect'
        if ($submit.Current.IsEnabled) { throw 'Empty fields enabled Connect' }
        Save-Window 'connector'
        (Find-Control 'Connector UUID').GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('invalid')
        (Find-Control 'API key').SetFocus()
        [System.Windows.Forms.SendKeys]::SendWait('test')
        Invoke-Control 'Connect'
        $null = Find-Control 'Enter a UUID in the format 00000000-0000-0000-0000-000000000000.'
        Invoke-Control 'Back'
        $null = Find-Control 'Sign in'
        Invoke-Control 'Cancel'
    }
} catch {
    Get-Content "$env:AGENTDUET_HOME/run/window.log" -ErrorAction SilentlyContinue | Out-Host
    Get-Process -Name 'Recorder','AgentDuet Recorder' -ErrorAction SilentlyContinue |
        Select-Object Id, ProcessName, SessionId, MainWindowTitle | Out-Host
    if ($window) {
        $window.FindAll($scope, [System.Windows.Automation.Condition]::TrueCondition) |
            ForEach-Object { Write-Host $_.Current.AutomationId $_.Current.Name }
    } else {
        $desktop.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition) |
            ForEach-Object { Write-Host $_.Current.ClassName $_.Current.Name }
    }
    Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddMinutes(-4)} -ErrorAction SilentlyContinue |
        Where-Object { $_.Message -match 'Recorder.exe' } | ForEach-Object { Write-Host $_.Message }
    throw
} finally {
    if ($reopened -and !$reopened.HasExited) { taskkill /pid $reopened.Id /t /f | Out-Null }
    if (!$launcher.HasExited) { taskkill /pid $launcher.Id /t /f | Out-Null }
}
