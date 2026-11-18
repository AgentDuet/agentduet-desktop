# AgentDuet Recorder on Windows

Qualification targets are Windows 10 22H2 and Windows 11 25H2 on x64. Recorder uses a native WinUI 3 window and a provisioned AgentDuet phone line. The installer includes the Windows App SDK and .NET runtime for the window. Calls are carried to the phone. The computer needs a network connection and writable local storage. Stereo WAV at 24 kHz and 16 bits uses about 346 MB per recorded hour, plus temporary space for the separate legs during capture and merge.

Run the Windows Recorder workflow manually for an internal installer. Install as a standard user, open AgentDuet Recorder from Start, review the documents, sign in through the browser or enter a connector UUID and API key, and choose a folder. The default instance lives in `%LOCALAPPDATA%\AgentDuet Recorder`. Closing the window leaves the recording engine running. Open Recorder from Start to return to your calls. The program is under `%LOCALAPPDATA%\Programs\AgentDuet Recorder`. Uninstall keeps the instance and recordings.

## Release qualification

On a clean installation of each target OS, record the OS build, CPU, RAM, free disk, installed size, workflow run ID and installer SHA-256. Minimum CPU and RAM and end-to-end Windows support are pending physical or VM qualification.

1. Install without elevation, including on a machine without a separately installed .NET desktop runtime. Cancel sign-in, retry, and complete it with a provisioned test account. Reject a read-only recordings folder and choose a writable one.
2. Make and receive a call. Speak a distinct phrase from each end. Verify direction, both voices, playback from the hub and from disk after relaunch, and the matching recording filename in the text header.
3. Disconnect the network, observe the line state, reconnect and record another call. Enable Start at Login, reboot, record before opening the hub, then reopen it while the daemon is running.
4. Upgrade over the installed version. Confirm the recordings and sign-in persist. Uninstall and verify the program and login entry are gone while the saved files remain.
5. For external distribution dispatch the workflow on main with signing enabled. CI uses WINDOWS_CERT_PFX (base64) and WINDOWS_CERT_PASSWORD. Verify the signed artifact on both platforms before attaching its run to the Recorder release. Compare the downloaded release asset's SHA-256 with the tested installer.
