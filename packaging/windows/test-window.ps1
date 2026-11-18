# Launch via the installed entry point against the running fixture daemon. Checking
# accessibility elements exercises WinUI, its packaged runtime, and authenticated setup.
param([Parameter(Mandatory)][string]$InstallDir)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$launcher = Start-Process "$InstallDir/AgentDuet Recorder.exe" -PassThru
try {
    $desktop = [System.Windows.Automation.AutomationElement]::RootElement
    $name = [System.Windows.Automation.AutomationElement]::NameProperty
    $title = [System.Windows.Automation.PropertyCondition]::new($name, 'AgentDuet Recorder')
    $agree = [System.Windows.Automation.PropertyCondition]::new($name, 'I agree')
    $browser = [System.Windows.Automation.PropertyCondition]::new($name, 'Sign in with browser')
    $scope = [System.Windows.Automation.TreeScope]::Descendants
    for ($i = 0; $i -lt 90; $i++) {
        if ($launcher.HasExited) { throw 'Installed launcher exited before setup appeared' }
        $window = $desktop.FindFirst([System.Windows.Automation.TreeScope]::Children, $title)
        if ($window) {
            $button = $window.FindFirst($scope, $agree)
            if ($button) { break }
        }
        Start-Sleep -Seconds 1
    }
    if (!$button) { throw 'Native terms screen did not appear' }
    $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    for ($i = 0; $i -lt 30; $i++) {
        if ($window.FindFirst($scope, $browser)) { return }
        Start-Sleep -Seconds 1
    }
    throw 'Native sign-in screen did not appear after accepting the terms'
} finally {
    if (!$launcher.HasExited) { taskkill /pid $launcher.Id /t /f | Out-Null }
}
