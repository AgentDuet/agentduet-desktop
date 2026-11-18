param([Parameter(Mandatory=$true)][string]$Artifact)
$ErrorActionPreference = 'Stop'
$bytes = [Convert]::FromBase64String($env:WINDOWS_CERT_PFX)
$certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    $bytes, $env:WINDOWS_CERT_PASSWORD,
    [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)
try {
    $signature = Set-AuthenticodeSignature -FilePath $Artifact -Certificate $certificate -HashAlgorithm SHA256 -TimestampServer 'http://timestamp.digicert.com'
    if ($signature.Status -ne 'Valid' -or !$signature.TimeStamperCertificate) {
        throw "Authenticode verification failed for $Artifact"
    }
} finally { $certificate.Dispose(); [Array]::Clear($bytes, 0, $bytes.Length) }
