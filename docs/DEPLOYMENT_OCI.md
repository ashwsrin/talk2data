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

1. **Create the `.oci` directory on the VM:**
   ```bash
   mkdir -p ~/.oci
   ```

2. **Copy your API key and config from your local machine to the VM:**
   From your **local machine** (the Mac), run:
   ```bash
   scp ~/.oci/config opc@<YOUR_VM_IP>:~/.oci/config
   scp ~/.oci/<YOUR_PRIVATE_KEY>.pem opc@<YOUR_VM_IP>:~/.oci/
   ```
   *(Be sure to replace `<YOUR_VM_IP>` and `<YOUR_PRIVATE_KEY>.pem` with the correct values).*

3. **Secure the files on the VM:**
   Back on the VM, ensure the key file has the correct permissions:
   ```bash
   chmod 600 ~/.oci/config
   chmod 600 ~/.oci/*.pem
   ```

4. **Verify the config:** 
   Ensure the `key_file` path inside `~/.oci/config` accurately points to the location of the `.pem` file on the VM (e.g., `/home/opc/.oci/your-key.pem`).

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

### Set up the `.env` file
1. Create a `.env` file containing your database secrets and API endpoint overrides:
   ```bash
   nano .env
   ```
2. Add your environment variables. Make sure your paths match the VM's file system:

   ```env
   # Oracle DB Connection
   ORACLE_NL2SQL_DSN=your_database_dsn_here
   ORACLE_NL2SQL_USER=your_db_username
   ORACLE_NL2SQL_PASSWORD=your_db_password
   # Required if mTLS is enabled
   ORACLE_NL2SQL_WALLET_PASSWORD=your_wallet_password
   
   # OCI Config overrides for the container
   OCI_CONFIG_FILE=/root/.oci/config
   OCI_CONFIG_PROFILE=DEFAULT
   
   # Note: To ensure your web browser correctly connects to the backend API,
   # you can configure Next.js to use your VM's public IP during deployment.
   NEXT_PUBLIC_API_URL=http://<YOUR_VM_PUBLIC_IP>:8001
   ```

---

## Step 5: Start the Services

Now you can build and run the multi-container Podman application in **detached mode (in the background)** using the `-d` flag. The `docker-compose.yml` file defaults to port 8001 for the backend.

If you have previously cloned the repository and it is currently running, you must stop the existing containers before pulling new changes and starting them again:

```bash
podman-compose down
```

To clean up all old containers, images, and volumes:
```bash
podman system prune -a -f
```



Then pull the latest changes that include the port 8001 update:
```bash
git pull origin main
```

Run the following command to start everything anew:

```bash
podman-compose up -d --build
podman-compose build --no-cache
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

---

## Step 6: Access the Application

The web frontend runs on port `3000` by default, and the backend runs on port `8001`. 

1. Ensure the VM's Security List or Network Security Group in OCI allows inbound traffic on ports `3000` and `8001` from your IP address.
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
