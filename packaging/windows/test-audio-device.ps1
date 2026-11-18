# The hosted runner has no speakers. Provision a signed, local virtual endpoint
# for the installed-app playback test. Nothing here goes into the installer.
$ErrorActionPreference = 'Stop'
Start-Service AudioEndpointBuilder
Start-Service Audiosrv
$dir = New-Item -ItemType Directory -Force "$env:RUNNER_TEMP/Recorder audio device"
$archive = "$dir/driver.zip"
Invoke-WebRequest 'https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack46.zip' -OutFile $archive
if ((Get-FileHash $archive).Hash -ne 'bc44e8974191a5a2427d6c979e2dab08dbb09925f3f435dfa2f0ba3e45b6eb90') {
    throw 'Unexpected virtual audio driver archive'
}
Expand-Archive $archive -DestinationPath $dir
$devcon = Get-ChildItem "${env:ProgramFiles(x86)}/Windows Kits/10/Tools/*/x64/devcon.exe" -ErrorAction SilentlyContinue | Select-Object -Last 1
if (!$devcon) {
    $wdk = "$dir/wdk.zip"
    Invoke-WebRequest 'https://api.nuget.org/v3-flatcontainer/microsoft.windows.wdk.x64/10.0.26100.6584/microsoft.windows.wdk.x64.10.0.26100.6584.nupkg' -OutFile $wdk
    $package = [IO.Compression.ZipFile]::OpenRead($wdk)
    try {
        $entry = $package.Entries | Where-Object FullName -like '*/x64/devcon.exe' | Select-Object -First 1
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, "$dir/devcon.exe")
    } finally { $package.Dispose() }
    $devcon = Get-Item "$dir/devcon.exe"
}
$signature = Get-AuthenticodeSignature $devcon.FullName
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation,') {
    throw 'Expected Microsoft-signed DevCon'
}
# VB-CABLE (vb-audio.com/Cable) is donationware, fetched only by the test VM.
$catalog = Get-AuthenticodeSignature "$dir/vbaudio_cable64_win10.cat"
if ($catalog.Status -ne 'Valid' -or $catalog.SignerCertificate.Subject -notmatch 'CN=Microsoft Windows Hardware Compatibility Publisher,') {
    throw 'Expected a Microsoft hardware-signed audio driver'
}
& $devcon.FullName install "$dir/vbMmeCable64_win10.inf" 'VBAudioVACWDM'
if ($LASTEXITCODE -ne 0) { throw 'Could not install the test speaker' }
