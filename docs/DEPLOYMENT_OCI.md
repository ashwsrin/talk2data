# Talk2Data Deployment Instructions - OCI Compute Instance (Oracle Linux 9.7)

This guide walks you through deploying the Talk2Data application to an Oracle Cloud Infrastructure (OCI) Compute Instance running **Oracle Linux Server 9.7**.

The deployment uses Podman and `podman-compose` (native to Oracle Linux) to run the backend, frontend, and MCP agents in a containerized environment. Authentication to OCI services is configured via a copied OCI configuration file (rather than Instance Principals).

---

## Prerequisites

- An OCI Compute Instance running Oracle Linux 9.7.
- SSH access to the VM.
- Access to the Oracle Autonomous Database Wallet file (if using mutual TLS for DB connection).

---

## Step 1: Install Dependencies (Podman & Git)

Oracle Linux 9 ships with Podman by default, which is an excellent drop-in replacement for Docker. We will install Git and `podman-compose`.

Connect to your instance via SSH and run the following commands:

```bash
# Update the system packages
sudo dnf update -y

# Install Git and the python package manager (pip)
sudo dnf install -y git python3-pip

# Install podman-compose globally via pip
sudo pip3 install podman-compose

# Verify the installation
podman version
podman-compose version
git --version
```

---

## Step 2: Configure OCI CLI Credentials

Since you are not using Instance Principals, you must manually copy your `.oci/config` file and the corresponding private key to the VM so the application's SDK can authenticate with OCI.

1. **Create a project-level `.oci` directory on the VM:**
   We recommend placing the OCI config inside the project directory so the path is easy to reference:
   ```bash
   mkdir -p ~/talk2data/.oci
   ```

2. **Copy your API key and config from your local machine to the VM:**
   From your **local machine** (the Mac), run:
   ```bash
   scp ~/.oci/config opc@<YOUR_VM_IP>:~/talk2data/.oci/config
   scp ~/.oci/<YOUR_PRIVATE_KEY>.pem opc@<YOUR_VM_IP>:~/talk2data/.oci/
   ```
   *(Replace `<YOUR_VM_IP>` and `<YOUR_PRIVATE_KEY>.pem` with the correct values).*

3. **Secure the files on the VM:**
   ```bash
   chmod 600 ~/talk2data/.oci/config
   chmod 600 ~/talk2data/.oci/*.pem
   ```

4. **Update `key_file` to use the container path:**
   The `.oci` directory is mounted to `/root/.oci` inside the Docker container. The `key_file` path in your config **must use the container path**, not the host path:
   ```bash
   nano ~/talk2data/.oci/config
   ```
   Ensure `key_file` uses an absolute container path:
   ```ini
   [DEFAULT]
   user=ocid1.user.oc1..xxxxx
   fingerprint=xx:xx:xx:xx
   tenancy=ocid1.tenancy.oc1..xxxxx
   region=us-chicago-1
   key_file=/root/.oci/<YOUR_PRIVATE_KEY>.pem
   ```
   > **⚠️ Important:** Do NOT use relative paths like `./key.pem` or host paths like `/home/opc/.oci/key.pem`. The OCI SDK reads this file inside the container where only `/root/.oci/` exists.

---

## Step 3: Clone the Repository

Clone the Talk2Data repository onto the VM.

```bash
# Clone the repo (replace with your personal GitHub token if private)
git clone https://github.com/ashwsrin/talk2data.git

# Enter the project directory
cd talk2data
```

---

## Step 4: Configure Environment Variables & Wallets

The application requires environment variables and the Oracle Database Wallet (if mTLS is enforced).

### Set up the Wallet
If you're using a secure Oracle DB (like an Autonomous Database) that requires a wallet:
1. Create a wallet directory:
   ```bash
   mkdir -p ~/wallet
   ```
2. Copy the wallet zip or extracted contents from your local machine to `~/wallet` on the VM.

### Set up the `.env` file (Backend + App DB)
1. Create a `.env` file containing your database secrets and configuration:
   ```bash
   nano .env
   ```
2. Add the following environment variables. **Replace placeholder values** with your actual credentials and VM IP:

   ```env
   # OCI SDK Configuration
   OCI_CONFIG_FILE=/root/.oci/config
   OCI_PROFILE=DEFAULT
   COMPARTMENT_ID=<YOUR_COMPARTMENT_OCID>

   # Host paths for Docker volume mounts
   # These map host directories into the container
   OCI_DIR=<ABSOLUTE_PATH_TO_.OCI_DIR>
   # Example: OCI_DIR=/home/opc/talk2data/.oci

   # Backend URL (used for attachment URLs returned by the API)
   BACKEND_URL=http://<YOUR_VM_PUBLIC_IP>:8001

   # CORS allowed origins (comma-separated; must include the frontend URL)
   CORS_ORIGINS=http://localhost:3000,http://<YOUR_VM_PUBLIC_IP>:3000

   # Application Database (Oracle ADB)
   ORACLE_DB_DSN=<your_app_db_dsn>
   ORACLE_DB_USER=<your_app_db_user>
   ORACLE_DB_PASSWORD=<your_app_db_password>
   ORACLE_WALLET_PATH=<ABSOLUTE_HOST_PATH_TO_WALLET>
   # Example: ORACLE_WALLET_PATH=/home/opc/wallet
   ORACLE_WALLET_PASSWORD=<your_wallet_password>
   ```

   > **Note:** `ORACLE_WALLET_PATH` in `.env` is the **host path**. The `docker-compose.yml` mounts it to `/wallet` inside containers and overrides the env var accordingly.

### Set up the `.env.nl2sql` file (NL2SQL MCP Server)
The NL2SQL MCP server uses a **separate** config file for its Oracle DB connection (which may point to a different schema/user than the app DB).

1. Create `.env.nl2sql`:
   ```bash
   nano .env.nl2sql
   ```
2. Add:
   ```env
   ORACLE_NL2SQL_DSN=<your_nl2sql_db_dsn>
   ORACLE_NL2SQL_USER=<your_nl2sql_db_user>
   ORACLE_NL2SQL_PASSWORD=<your_nl2sql_db_password>
   ORACLE_NL2SQL_WALLET_PATH=/wallet
   ORACLE_NL2SQL_WALLET_PASSWORD=<your_wallet_password>
   ```

### Update the `app_settings` table (after first startup)
After the application starts for the first time, you can customise the **System Prompt** via the Settings page (`http://<YOUR_VM_PUBLIC_IP>:3000/settings`). This is the only setting stored in the database; all other configuration is managed via `.env` files.

---

## Step 5: Fix SELinux Permissions (Oracle Linux)

Oracle Linux 9 enforces **SELinux** by default, which blocks containers from reading host-mounted volumes. You must relabel the mounted directories so Podman can access them.

```bash
# Relabel OCI config directory
sudo chcon -Rt svirt_sandbox_file_t <PATH_TO_.OCI_DIR>
# Example: sudo chcon -Rt svirt_sandbox_file_t /home/opc/talk2data/.oci

# Relabel the private key file explicitly
sudo chcon -t svirt_sandbox_file_t <PATH_TO_.OCI_DIR>/*.pem

# Relabel wallet directory
sudo chcon -Rt svirt_sandbox_file_t <PATH_TO_WALLET>
# Example: sudo chcon -Rt svirt_sandbox_file_t /home/opc/wallet
```

> **Tip:** If you get `ConfigFileNotFound` or `InvalidKeyFilePath` errors after starting, SELinux is likely blocking access. You can temporarily test with `sudo setenforce 0` (permissive mode) to confirm, then re-enable with `sudo setenforce 1` after applying the labels above.

---

## Step 6: Start the Services

Now you can build and run the multi-container Podman application in **detached mode (in the background)** using the `-d` flag.

If you have previously cloned the repository and it is currently running, you must stop the existing containers before pulling new changes and starting them again:

```bash
podman-compose down
```

To clean up all old containers, images, and volumes:
```bash
podman system prune -a -f
```

Then pull the latest changes:
```bash
git pull origin main
```

Run the following command to start everything anew:

```bash
podman-compose up -d --build
```

### Checking the Status

To ensure all services (web, app backend, MCPs) started successfully in the background:
```bash
podman-compose ps
```

To view the trailing logs for all services and ensure there are no errors:
```bash
podman-compose logs -f
```
*(Press `Ctrl+C` to exit the screen; the services will continue running in the background!)*

### Updating After Code Changes

For **backend-only** changes (Python files), rebuild only the `app` service:
```bash
git pull origin main
podman-compose up -d --build app
```

For **frontend** changes (Next.js), rebuild the `web` service:
```bash
git pull origin main
podman-compose up -d --build web
```

For **`.env` changes only** (no code changes), restart without rebuilding:
```bash
podman-compose down && podman-compose up -d
```

---

## Step 7: Access the Application

The web frontend runs on port `3000` by default, and the backend runs on port `8001`. 

1. Ensure the VM's Security List or Network Security Group in OCI allows inbound traffic on port `3000` from your IP address.
2. Ensure the VM's internal firewall (firewalld) permits traffic.
   ```bash
   sudo firewall-cmd --permanent --add-port=3000/tcp
   sudo firewall-cmd --permanent --add-port=8001/tcp
   sudo firewall-cmd --reload
   ```

Open your browser and navigate to:
```
http://<YOUR_VM_PUBLIC_IP>:3000
```

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `ConfigFileNotFound: /root/.oci/config` | SELinux blocking volume mount | Run `sudo chcon -Rt svirt_sandbox_file_t <OCI_DIR>` |
| `InvalidKeyFilePath: ./key.pem` | `key_file` uses relative/host path | Change to `/root/.oci/<key>.pem` in the config |
| `Failed to fetch` in browser | Frontend calling backend directly | Ensure `apiBaseUrl` stays empty (relative URLs); rebuild `web` |
| `ERR_CONNECTION_TIMEOUT` on port 8001 | Port 8001 not reachable from browser | This is expected; frontend proxies via port 3000 |
| `CORS error` in browser console | VM IP not in `CORS_ORIGINS` | Add `http://<VM_IP>:3000` to `CORS_ORIGINS` in `.env` and restart |
| Container exits immediately | Check logs with `podman logs <container>` | Fix the reported error, then restart |


---

## Connect to removte VM
```bash
ssh -o User=opc -o IdentityFile=/Users/ashwins/Desktop/T2D/V3/VMKeys/ssh-key-2026-02-19.key -o ServerAliveInterval=60 -o ServerAliveCountMax=5 144.24.132.61
```

## Tunneling to remote VM and opening the app locally
```bash
ssh -fN -i "/Users/ashwins/Desktop/T2D/V3/VMKeys/ssh-key-2026-02-19.key" -L 127.0.0.1:3002:127.0.0.1:3000 opc@144.24.132.61
```


## Copy files to VM
```bash
scp -r -o User=opc -o IdentityFile=/Users/ashwins/Desktop/T2D/V3/VMKeys/ssh-key-2026-02-19.key -o ServerAliveInterval=60 -o ServerAliveCountMax=5 /Users/ashwins/Desktop/T2D/Wallet_TECPDATP01 144.24.132.61:/home/opc/talk2dataclient/talk2data/Wallet_TECPDATP01
```

## Kill Rogue Processes
```bash
lsof -i :8000
kill -9 16484
```
