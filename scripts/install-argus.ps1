$ErrorActionPreference = "Stop"

$Repo = if ($env:ARGUS_GITHUB_REPO) { $env:ARGUS_GITHUB_REPO } else { "yym68686/argus" }
$Ref = if ($env:ARGUS_GITHUB_REF) { $env:ARGUS_GITHUB_REF } else { "main" }
$Version = if ($env:ARGUS_VERSION) { $env:ARGUS_VERSION } else { "" }
$BinDir = if ($env:ARGUS_BIN_DIR) { $env:ARGUS_BIN_DIR } else { Join-Path $HOME ".argus\bin" }
$BinPath = Join-Path $BinDir "argus.exe"

function Resolve-Target {
  $Arch = $env:PROCESSOR_ARCHITECTURE
  if ([string]::IsNullOrWhiteSpace($Arch)) {
    $Arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
  }
  switch -Regex ($Arch.ToUpperInvariant()) {
    "ARM64|AARCH64" { return "windows-arm64" }
    "AMD64|X64|X86_64" { return "windows-amd64" }
    default { throw "Unsupported Windows architecture: $Arch" }
  }
}

function Add-ToUserPath([string]$PathToAdd) {
  $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if ([string]::IsNullOrWhiteSpace($UserPath)) {
    $UserPath = ""
  }
  $PathParts = $UserPath -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
  if (-not ($PathParts | Where-Object { $_.TrimEnd('\') -ieq $PathToAdd.TrimEnd('\') })) {
    $NewUserPath = if ([string]::IsNullOrWhiteSpace($UserPath)) { $PathToAdd } else { "$PathToAdd;$UserPath" }
    [Environment]::SetEnvironmentVariable("Path", $NewUserPath, "User")
  }
  if (-not (($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -ieq $PathToAdd.TrimEnd('\') })) {
    $env:Path = "$PathToAdd;$env:Path"
  }
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$Target = Resolve-Target

if ([string]::IsNullOrWhiteSpace($Version)) {
  $VersionUrl = "https://raw.githubusercontent.com/$Repo/$Ref/VERSION"
  try {
    $Version = (Invoke-WebRequest -UseBasicParsing -Uri $VersionUrl).Content.Trim()
  } catch {
    $Version = ""
  }
}

$Installed = $false
if (-not [string]::IsNullOrWhiteSpace($Version)) {
  $ReleaseUrl = "https://github.com/$Repo/releases/download/v$Version/argus-$Target.exe"
  try {
    Invoke-WebRequest -UseBasicParsing -Uri $ReleaseUrl -OutFile $BinPath
    $Installed = $true
  } catch {
    $Installed = $false
  }
}

if (-not $Installed) {
  $Go = Get-Command go -ErrorAction SilentlyContinue
  if (-not $Go) {
    throw "GitHub release binary was not available and Go is not installed. Create a v$Version release or install Go, then rerun."
  }
  $Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("argus-install-" + [Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
  try {
    $Git = Get-Command git -ErrorAction SilentlyContinue
    if ($Git) {
      & $Git.Source clone --depth 1 --branch $Ref "https://github.com/$Repo.git" (Join-Path $Tmp "repo")
    } else {
      $Tarball = Join-Path $Tmp "src.tar.gz"
      Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$Repo/archive/$Ref.tar.gz" -OutFile $Tarball
      New-Item -ItemType Directory -Force -Path (Join-Path $Tmp "repo") | Out-Null
      tar -xzf $Tarball -C (Join-Path $Tmp "repo") --strip-components 1
    }
    Push-Location (Join-Path $Tmp "repo\apps\node-host")
    try {
      & $Go.Source build -trimpath -ldflags "-s -w" -o $BinPath ./cmd/argus
    } finally {
      Pop-Location
    }
  } finally {
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
  }
}

Add-ToUserPath $BinDir
Write-Host "Installed argus CLI: $BinPath"
& $BinPath --help | Out-Null
exit $LASTEXITCODE
