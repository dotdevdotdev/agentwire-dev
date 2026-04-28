# Remote Access Setup

Access your AgentWire portal from anywhere using Cloudflare Tunnel with Zero Trust authentication.

## Overview

This guide covers:
1. **Cloudflare Tunnel** - Secure outbound-only connection to expose your portal
2. **Cloudflare Access** - Zero Trust authentication (email OTP, SSO, etc.)
3. **SSH Tunnels** - Forwarding remote services (e.g., TTS on a GPU server)

## Prerequisites

- A domain managed by Cloudflare (free tier works)
- `cloudflared` CLI installed
- AgentWire portal running locally

## 1. Install cloudflared

```bash
# macOS
brew install cloudflared

# Linux
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
chmod +x cloudflared
sudo mv cloudflared /usr/local/bin/
```

## 2. Authenticate with Cloudflare

```bash
cloudflared tunnel login
```

This opens a browser to authenticate and stores credentials in `~/.cloudflared/`.

## 3. Create a Tunnel

```bash
cloudflared tunnel create agentwire
```

Note the tunnel ID (UUID) - you'll need it for configuration.

## 4. Configure the Tunnel

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: YOUR_TUNNEL_ID
credentials-file: /Users/YOUR_USER/.cloudflared/YOUR_TUNNEL_ID.json

ingress:
  - hostname: agentwire.yourdomain.com
    service: https://localhost:8765
    originRequest:
      noTLSVerify: true  # Portal uses self-signed cert
  - service: http_status:404
```

## 5. Create DNS Record

```bash
cloudflared tunnel route dns agentwire agentwire.yourdomain.com
```

This creates a CNAME record pointing to your tunnel.

## 6. Set Up Cloudflare Access

Cloudflare Access adds authentication before anyone can reach your portal.

### Create Access Application

1. Go to [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/)
2. Navigate to **Access → Applications**
3. Click **Add an application** → **Self-hosted**
4. Configure:
   - **Application name**: AgentWire Portal
   - **Session duration**: 24 hours (or your preference)
   - **Application domain**: `agentwire.yourdomain.com`

### Create Access Policy

1. Go to **Access → Policies**
2. Click **Create a policy**
3. Configure:
   - **Policy name**: Allow Me
   - **Action**: Allow
   - **Include rule**: Emails = `your-email@example.com`
4. Save the policy
5. Go back to your application and **attach the policy**

Now accessing `agentwire.yourdomain.com` requires email verification.

## 7. Run the Tunnel

### Manual (for testing)

```bash
cloudflared tunnel run agentwire
```

### As a Service (persistent)

**macOS:**

```bash
# Copy config to system location
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/config.yml /etc/cloudflared/
sudo cp ~/.cloudflared/YOUR_TUNNEL_ID.json /etc/cloudflared/

# Update config to use system paths
sudo sed -i '' 's|/Users/YOUR_USER/.cloudflared/|/etc/cloudflared/|' /etc/cloudflared/config.yml

# Install service
sudo cloudflared service install

# Start service
sudo launchctl load /Library/LaunchDaemons/com.cloudflare.cloudflared.plist
```

**Linux (systemd):**

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

## 8. SSH Tunnels for Remote Services

If your TTS or other services run on a different machine, set up SSH port forwarding.

### Manual Tunnel

```bash
# Forward TTS port from remote-server to localhost
ssh -f -N -L 8100:localhost:8100 remote-server
```

### Persistent SSH Tunnel (autossh)

```bash
# Install autossh
brew install autossh  # macOS
apt install autossh   # Linux

# Create persistent tunnel
autossh -M 0 -f -N -L 8100:localhost:8100 remote-server
```

### As a launchd Service (macOS)

Create `~/Library/LaunchAgents/com.agentwire.tts-tunnel.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.agentwire.tts-tunnel</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/ssh</string>
        <string>-N</string>
        <string>-L</string>
        <string>8100:localhost:8100</string>
        <string>remote-server</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.agentwire.tts-tunnel.plist
```

## Verification

1. **Check tunnel status:**
   ```bash
   cloudflared tunnel info agentwire
   ```

2. **Test remote access:**
   - Open `https://agentwire.yourdomain.com` on your phone
   - Complete Cloudflare Access authentication
   - Verify portal loads and voice works

3. **Check TTS connectivity:**
   ```bash
   curl http://localhost:8100/health
   ```

## Troubleshooting

### Tunnel shows "no active connections"

- Check if cloudflared is running: `ps aux | grep cloudflared`
- Check logs: `tail -f /Library/Logs/com.cloudflare.cloudflared.err.log`
- Verify config paths are correct in `/etc/cloudflared/config.yml`

### Access emails not arriving

- Check spam folder
- Verify the policy is **attached to the application** (not just created)
- Check "Used by applications" count in policy details

### TTS not working remotely

- Verify SSH tunnel is running: `ps aux | grep "ssh.*8100"`
- Test TTS health: `curl http://localhost:8100/health`
- Check portal logs for TTS connection errors

### WebSocket not connecting

- Cloudflare Tunnels support WebSockets by default
- Check browser console for connection errors
- Verify the portal shows "Connected" status

## Failsafe Access (Emergency Recovery)

If the portal, claude, or other tooling breaks, you need a way to get in and fix things. This section covers browser-based SSH and VNC access that works from any device.

### What's Set Up

| Service | URL | Purpose |
|---------|-----|---------|
| **Browser SSH** | `https://ssh.solodev.dev` | Terminal access from any browser |
| **Browser VNC** | `https://vnc.solodev.dev` | GUI desktop from any browser |
| **Termux SSH** | `ssh mac` | Proper terminal from Android |

### macOS Requirements

Enable in **System Settings → General → Sharing**:

1. **Remote Login** (SSH) - ON
2. **Screen Sharing** (VNC) - ON
   - Click ⓘ → Enable "VNC viewers may control screen with password"
   - Set a VNC password

### Tunnel Configuration

The tunnel config (`/etc/cloudflared/config.yml`) includes:

```yaml
ingress:
  # AgentWire portal
  - hostname: agentwire.solodev.dev
    service: https://localhost:8765
    originRequest:
      noTLSVerify: true

  # SSH browser-based access (failsafe)
  - hostname: ssh.solodev.dev
    service: ssh://localhost:22

  # VNC browser-based access (GUI failsafe)
  - hostname: vnc.solodev.dev
    service: tcp://localhost:5900

  - service: http_status:404
```

### Cloudflare Access Applications

Create two Access applications with **browser rendering** enabled:

| Application | Domain | Browser Rendering |
|-------------|--------|-------------------|
| SSH Failsafe | `ssh.solodev.dev` | **SSH** |
| VNC Remote Desktop | `vnc.solodev.dev` | **VNC** |

Both should have an Access policy allowing your email.

**Important:** Disable "Allow auto verification with Cloudflare" in both apps to avoid UI glitches in the browser terminal.

### Termux Setup (Android)

Install Termux from [F-Droid](https://f-droid.org/packages/com.termux/) (not Play Store).

```bash
# Install SSH and cloudflared
pkg install openssh cloudflared

# Authenticate (opens browser)
cloudflared access ssh --hostname ssh.solodev.dev

# Add to SSH config for easy access
mkdir -p ~/.ssh
cat >> ~/.ssh/config << 'EOF'
Host mac
    HostName ssh.solodev.dev
    User dotdev
    ProxyCommand cloudflared access ssh --hostname %h
EOF

# Now just:
ssh mac
```

### Services That Survive Reboot

| Service | Type | Location |
|---------|------|----------|
| **Cloudflared tunnel** | LaunchDaemon | `/Library/LaunchDaemons/com.cloudflare.cloudflared.plist` |
| **Caffeinate** | LaunchAgent | `~/Library/LaunchAgents/com.user.caffeinate.plist` |
| **Remote Login** | System | Enabled in System Settings |
| **Screen Sharing** | System | Enabled in System Settings |

### What Happens on Reboot

1. Mac boots up
2. `caffeinate` starts → prevents sleep
3. `cloudflared` starts → connects tunnel to Cloudflare
4. Remote Login and Screen Sharing are ready on-demand
5. Within ~30 seconds, all failsafe URLs are accessible

### Verifying Services

```bash
# Check all services
pgrep -f cloudflared && echo "Tunnel: OK" || echo "Tunnel: DOWN"
pgrep caffeinate && echo "Caffeinate: OK" || echo "Caffeinate: DOWN"

# Check tunnel logs
sudo tail -20 /Library/Logs/com.cloudflare.cloudflared.err.log
```

### Recovery Scenarios

| Problem | Solution |
|---------|----------|
| Portal not responding | SSH in via `ssh.solodev.dev` → `agentwire portal restart` |
| Claude binary corrupted | SSH in → `brew reinstall claude` or download fresh |
| Can't type in browser SSH | Use Termux `ssh mac` or browser VNC instead |
| VNC shows "connection closed" | Check Screen Sharing is enabled, VNC password is set |
| Tunnel not connecting | SSH locally or physically → check `cloudflared` logs |
| Mac went to sleep | Shouldn't happen (caffeinate), but wake via VNC or physical |

### Browser SSH Limitations

Cloudflare's browser SSH terminal has quirks:
- Long commands may display garbled (but execute correctly)
- Use it for emergencies, not daily work
- For real work, use Termux `ssh mac` or browser VNC

## Security Notes

- **Cloudflare Tunnel** only allows outbound connections - no open ports on your firewall
- **Cloudflare Access** adds authentication before traffic reaches your portal
- **SSH tunnels** should use key-based authentication, not passwords
- Consider adding additional Access policies (IP restrictions, device posture, etc.)
