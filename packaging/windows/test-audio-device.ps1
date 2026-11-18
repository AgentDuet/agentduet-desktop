# The hosted runner has no speakers. Provision a signed, local virtual endpoint
# for the installed-app playback test. Nothing here goes into the installer.
$ErrorActionPreference = 'Stop'
Start-Service AudioEndpointBuilder
Start-Service Audiosrv
$dir = New-Item -ItemType Directory -Force "$env:RUNNER_TEMP/Recorder audio device"
$archive = "$dir/driver.zip"
Invoke-WebRequest 'https://github.com/VirtualDrivers/Virtual-Audio-Driver/releases/download/25.7.14/Virtual.Audio.Driver.Signed.-.25.7.14.zip' -OutFile $archive
if ((Get-FileHash $archive).Hash -ne 'dd10560994de65a7e587fb8b93c0d7e9838292d9c3566a0976c2786d727292bd') {
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
$inf = Get-ChildItem $dir -Recurse -Filter VirtualAudioDriver.inf
& $devcon.FullName install $inf.FullName 'ROOT\VirtualAudioDriver'
if ($LASTEXITCODE -ne 0) { throw 'Could not install the test speaker' }
