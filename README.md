# Hikvision Custom Recovery TFTP Server

# WIP - I will be porting this to something more crossplatform. Rtd hikvision engineers cannot make a software that can self-update, proper cross-platform web ui or update tool.

[![.NET 8.0](https://img.shields.io/badge/.NET-8.0-blueviolet.svg)](https://dotnet.microsoft.com/download/dotnet/8.0)
[![Language: C#](https://img.shields.io/badge/Language-C%23-green.svg)]()
[![Native AOT](https://img.shields.io/badge/Native_AOT-Supported-success.svg)](#native-aot-compilation)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows_-blue.svg)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE.txt)

A high-performance, asynchronous, and ultra-lightweight TFTP server specifically engineered to unbrick, recover, and flash firmware onto **Hikvision IP cameras**. 

Unlike standard TFTP servers, this tool implements the proprietary **Hikvision Handshake protocol** and handles modern large firmware files (>32MB) flawlessly.

---
* Download Link: https://github.com/Den34DK/Hikvision-Recovery-TFTP/releases/
---
## 🔍 Core Breakthroughs & Features

### 1. The Proprietary "Hikvision Magic Knock" (Port 9978) Bypass
Before starting any standard TFTP transmission, Hikvision bootloaders send a UDP broadcast probe to port **`9978`** with the payload signature `SWKH` (`53 57 4b 48` in hex). 
* **The Problem:** If the server does not receive this packet and echo it back to the camera, **the camera will never send a TFTP request (Port 69)**. It will simply skip the recovery mode and boot normally. Standard TFTP utilities (like Tftpd64) do not support this, which is why they fail with modern cameras.
* **Solution:** This server features a built-in **Handshake listener on port 9978** that instantly echoes the `SWKH` knock back to the camera, prompting it to immediately start the TFTP firmware upload.

### 2. The >32MB Rollover Problem (RFC 1350)
Standard TFTP transmits files in 512-byte blocks. The block counter is stored in a 16-bit unsigned integer (`ushort`), capping at **65,535 blocks**. 

$$\text{Maximum Standard File Size} = 65,535 \times 512 \text{ bytes} \approx 33.55 \text{ MB}$$

When modern cameras firmwares exceed this size, legacy TFTP servers fail because they don't handle block number overflow.
* **Solution:** This server implements safe `unchecked` block increments. When the counter reaches `65,535`, it rolls over back to `0` seamlessly, allowing you to flash files of **any size (100MB+)**.

### 3. Infinite Bootloop Prevention (Recently Flashed Blacklist)
Once a camera successfully downloads the firmware and restarts, it may send the `SWKH` knock again. If the server answers, the camera is forced back into recovery mode, causing an **infinite bootloop**.
* **Solution:** The server dynamically tracks successfully flashed camera IPs and places them on a temporary **10-minute blacklist**. The server ignores repeated knocks from these IPs, allowing the camera to boot up normally.
---
## ✨ Features

*   **Automatic Interface Binding:** Binds to `0.0.0.0` (Any IP), solving the "Media Disconnected / Link Down" Windows startup problem when the camera is offline at server boot.
*   **Auto-Fallback Redirection:** If a camera requests a model-specific file (like `digicap_E10.dav`) which is missing, the server replies with a precise TFTP Error 0 ("No such file or directory"). This gracefully forces the camera to request the standard `digicap.dav`.
*   **Dynamic Console UI:** Real-time single-line progress bar showing upload progress and speed (MB/s). It minimizes console lag during high-speed transfers.
*   **Automatic Logging:** Every console event is safely written to `log.txt` in real-time using a thread-safe `DoubleWriter` class.
*   **Native AOT Compilation:** Compiles down to a standalone, ultra-small binary (~3 MB) with no external dependencies (no .NET Runtime required on the host PC).
---

## ⚙️ Hikvision Default Network Settings

Set up your PC's network card statically to match these parameters:

| Parameter | Required Value | Notes |
| :--- | :--- | :--- |
| **PC (TFTP Server) IP** | `192.168.1.128` | **Must be exact.** Set this statically in Windows. *(See legacy note)* |
| **PC Subnet Mask** | `255.255.255.0` | Standard Class C subnet mask. |
| **Camera Recovery IP** | `192.168.1.64` | Assigned automatically by the camera bootloader. |
| **Handshake Port** | `9978` | UDP Port (must be free). |
| **TFTP Server Port** | `69` | UDP Port (requires admin privileges). |
| **Firmware Filename** | `digicap.dav` | **Case-sensitive.** Place in the application folder. |

> 💡 **Legacy Subnet Note:** Older Hikvision cameras (pre-2015) use the `192.0.0.x` subnet. If flashing an older model, set your static PC IP to **`192.0.0.128`** (the camera will assume `192.0.0.64`).

---
### 📷 Supported Cameras
This tool is designed to work with all Hikvision/HiWatch IP cameras that utilize the standard TFTP recovery protocol. Below is a list of models that have been **actively verified** to work.

<details>
<summary>See verified models</summary>
   
- **HiWatch DS-I400(D)**
- **HiWatch DS-I450L(C)**
- **Hikvision DS-2CD2043G2-I(U)**

*Don't see your camera on the list? It will likely work if your device uses the standard Hikvision bootloader.*
</details>

---

## 🚀 Step-by-Step Flashing Guide

1.  **Configure PC Network Adapter:**
    *   Disable all VM virtual adapters, VPNs, Wi-Fi, and secondary network cards.
    *   Configure your primary Ethernet adapter statically: IP **`192.168.1.128`** (or **`192.0.0.128`**), Subnet: **`255.255.255.0`**, Gateway: blank.
2.  **Disable Windows Firewall:**
    *   Temporarily disable Windows Defender Firewall, as it will block incoming UDP traffic on ports 69 and 9978.
3.  **Deploy Firmware:**
    *   Place your official firmware file in the program folder and rename it exactly to `digicap.dav`.
4.  **Run Server:**
    *   Launch `Hikvision-Recovery-TFTP.exe` as **Administrator**.
5.  **Power Cycle the Camera:**
    *   Turn the camera off. Plug it into the same switch/PoE injector as your PC.
    *   Power the camera on. 
    *   Watch the console: you will see the `SWKH` handshake, file request redirection, and then a clean progress bar.
6.  **Post-Transfer Actions:**
    *   Once the upload reaches 100%, the server will print a success message (e.g., `Success: 'digicap.dav' uploaded in 15.1s!`).
    *   Following this, the server will process internal operations and print a message indicating that the camera's knock was detected but temporarily ignored (e.g., `[HANDSHAKE] [SWKH] Knock from 192.168.1.69 detected, but ignored (recently flashed).`).
    *   **Crucially, only after you see the `ignored (recently flashed)` message in the server log, wait for 10-20 seconds.**
    *   Then, power cycle the camera (disconnect and reconnect power).
    *   The camera should then boot up normally with its default IP address (usually `192.168.1.64` or `192.168.1.120`).
---

## ⚠️ Troubleshooting

*   **Error: UDP Port 69 or 9978 is already in use**
    *   Close standard TFTP tools, Tftpd64, or the official Hikvision Auto-Update utility. Ensure no other service is binding to these ports.

*   **Nothing happens when the camera boots**
    *   If you see UDP packets on port 9978 from the camera but no responses, your **firewall is active** or your PC is not set to `192.168.1.128`.
    *   Always use a **network switch or PoE injector** between your PC and the camera. Direct cable connection is highly discouraged due to network link-up delay on Windows.

*   **The upload finishes but the camera keeps requesting files**
    *   The camera rejected the firmware (wrong region, corrupted file header, or incorrect hardware model). Download the correct official firmware and try again.

---

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
