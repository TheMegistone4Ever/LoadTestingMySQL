# Load Testing the `parcel_delivery` Database

###### &emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp; — by [Mykyta Kyselov (TheMegistone4Ever)](https://github.com/TheMegistone4Ever).

Software Design practical assignment: dataset generation, MySQL load testing, and testbed monitoring.  
The project is built entirely on **uv** — `pip` is not used. MySQL and the monitoring stack run in Docker.

```bash
docker compose --project-directory . -f docker/docker-compose.mysql.yml -f docker/docker-compose.monitoring.yml up -d
```

## Table of Contents

1. [Architecture](#1-architecture)
2. [Prerequisites](#2-prerequisites)
3. [Environment Setup](#3-environment-setup)
4. [Execution Steps](#4-execution-steps)
    1. [Start the Testbed](#41-start-the-testbed)
    2. [Connect Database in PyCharm (Optional)](#42-connect-database-in-pycharm-optional)
    3. [Populate the Database](#43-populate-the-database)
    4. [Run the Load Test](#44-run-the-load-test)
    5. [Saturation Point Analysis (Optional)](#45-saturation-point-analysis-optional)
    6. [Stop the Testbed](#46-stop-the-testbed)
5. [Results](#5-results)
6. [PyCharm Run Configurations](#6-pycharm-run-configurations)
7. [Troubleshooting](#7-troubleshooting)
8. [Hardware / Test Setup Verification](#8-hardware--test-setup-verification)
9. [License](#9-license)

---

## 1 Architecture

```
load_testing/
├── pyproject.toml                      # project dependencies (single source of truth)
├── uv.lock                             # pinned lockfile versions
├── .env                                # shared configuration for Compose and scripts
│
├── database_connection_settings.py     # MySQL connection settings (environment variables)
├── parcel_dataset_specification.py     # dataset volumes, status distribution, tracking & phone formulas
├── console_encoding.py                 # UTF-8 console output for Windows
│
├── sql/
│   ├── mysql_server_settings.sql       # buffer pool, redo log, max_connections (for reference)
│   ├── mysql_monitoring_user.sql       # user for mysqld_exporter
│   └── parcel_delivery_schema.sql      # DDL: tables; indexes and FKs added after seeding
├── seed_fake_parcel_delivery_data.py   # step 2: generate and seed ≈99M rows
│
├── load_test_plan.py                   # step 3: stages, requirement thresholds, saturation steps
├── locustfile_parcel_delivery.py       # step 4: 7 transactions + stage plan (LoadTestShape)
├── load_test_requirements_check.py     # stage measurements and pass/fail verdict ✔/✘
├── saturation_analysis.py              # saturation curve: peak, inflection point, operational capacity
├── prometheus_metrics_exporter.py      # Locust and host metrics for Prometheus (port 9646)
├── run_load_test.py                    # test entry point: master + N workers
├── run_all.ps1                         # end-to-end run: testbed + dataset + requirement stages
├── run_saturation_test.ps1             # standalone run: 100 → 5000 stepped test & saturation curve
│
├── docker/
│   ├── docker-compose.mysql.yml        # MySQL 9.7 — target under test
│   └── docker-compose.monitoring.yml   # Prometheus + Grafana + mysqld_exporter
├── monitoring/
│   ├── prometheus.yml
│   └── grafana/
│       ├── provisioning/datasources/prometheus_datasource.yml
│       ├── provisioning/dashboards/dashboard_provider.yml
│       └── dashboards/parcel_delivery_load_test_dashboard.json
│
└── load_test_results/<timestamp>/      # created after each test run
```

Data flows:

```
Windows (native)                      Docker (load_testing_default network)
─────────────────────                 ────────────────────────────────────
seed_fake_parcel_delivery_data.py ──► mysql :3306  ◄── mysqld_exporter :9104
                                            ▲                    │
run_load_test.py                            │                    ▼
  └─► Locust master ─► N workers ───────────┘            Prometheus :9090
         └─ exporter :9646 ◄────── host.docker.internal ─────────┘
                                                                 ▼
                                                           Grafana :3000
```

Entity-relationship diagram of the `parcel_delivery` database:
<img src="diagrams/parcels.svg" alt="ERD of the parcel_delivery database" width="100%">

```


## 2 Prerequisites

| Component      | Purpose                                     |
|----------------|---------------------------------------------|
| uv             | Dependency management and script execution  |
| Docker Desktop | MySQL, Prometheus, Grafana, mysqld_exporter |

There is no need to install MySQL locally — it runs via `docker/docker-compose.mysql.yml`.

## 3 Environment Setup

```powershell
uv sync
```

Creates `.venv` based on `pyproject.toml` and `uv.lock`.

Credentials and host addresses are defined in `.env` — read automatically both by `docker compose` (via
`--project-directory .`) and by Python scripts (via `uv run --env-file .env`).
The default root password is `parcel_delivery_root`.  
Make sure to change it **before** starting the container for the first time: once initialized, it is stored in the
`mysql_data` volume, and changing it later will require deleting the volume.

## 4 Execution Steps

### 4.1 Start the Testbed

All `docker compose` commands must be executed **from the project root**: `--project-directory .` sets the root as the
base path for relative file paths in Compose files and loads `.env`.

```powershell
docker compose --project-directory . -f docker/docker-compose.mysql.yml -f docker/docker-compose.monitoring.yml up -d
```

The initial MySQL startup takes ~20 seconds: an 8 GB buffer pool and 4 GB redo log are allocated, and
`sql/mysql_monitoring_user.sql` runs automatically to provision the mysqld_exporter user.

**Verification:**

* `http://localhost:9090/targets` — the `mysql` target must be UP; `locust` stays DOWN until a test is running.
* `http://localhost:3000` (admin / admin) → Dashboards → Load Testing → "Load Testing БД parcel_delivery".

### 4.2 Connect Database in PyCharm (Optional)

View → Tool Windows → Database → **+** → Data Source → MySQL:

| Field    | Value                                                       |
|----------|-------------------------------------------------------------|
| Host     | `127.0.0.1`                                                 |
| Port     | `3306`                                                      |
| User     | `root`                                                      |
| Password | `parcel_delivery_root` (`MYSQL_PASSWORD` value from `.env`) |
| Database | `parcel_delivery`                                           |

If PyCharm prompts you to download the driver, click Download, then Test Connection → OK.

`sql/mysql_server_settings.sql` does not need to be executed when running in Docker: these settings are already passed
via flags in `docker/docker-compose.mysql.yml`. The file is kept for reporting purposes — the `SELECT` query at the
bottom confirms that the 8 GB buffer pool, 4 GB redo log, and `max_connections = 6000` were applied.

### 4.3 Populate the Database

```powershell
uv run --env-file .env seed_fake_parcel_delivery_data.py --scale 0.1   # smoke run, ≈10M rows
uv run --env-file .env seed_fake_parcel_delivery_data.py               # full dataset, 15–30 min
```

Each run drops and recreates the `parcel_delivery` database from scratch.

### 4.4 Run the Load Test

```powershell
uv run --env-file .env run_load_test.py --workers 8 --minutes-per-stage 2   # smoke test, ~15 min
uv run --env-file .env run_load_test.py --workers 8                         # full run, ≈2 h 20 min
```

Useful flags: `--stages warmup_100,load_500` runs only the selected stages.

### 4.5 Saturation Point Analysis (Optional)

Requirement stages progress in large increments (500 → 1000 → 2000 → 5000), which demonstrates that the system degrades
somewhere between 2000 and 5000 users, but does not pinpoint the exact threshold. The second plan, `saturation`, uses
finer increments (14 stages: 100, 250, 500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, and recovery) to
construct the throughput curve:

```powershell
.\run_saturation_test.ps1                      # full stepped test, ≈1 h 35 min
.\run_saturation_test.ps1 -MinutesPerStage 3   # fast curve, ≈50 min
```

The same without the wrapper script: `uv run --env-file .env run_load_test.py --plan saturation --workers 8`.

Results are written to an isolated directory `load_test_results/<timestamp>_saturation/` to avoid mixing with standard
requirement runs. The database is **not** re-seeded by default: the stepped test measures its own baseline at `load_500`
within the same run, making the curve self-contained. Use `-Reseed` only if you need to reset the dataset to its initial
volume.

Do not compare points across different runs: transactions T5 and T6 insert new records during testing, causing the
`parcels` table to grow significantly. The curve is only valid within a single execution run.

### 4.6 Stop the Testbed

```powershell
docker compose --project-directory . -f docker/docker-compose.mysql.yml -f docker/docker-compose.monitoring.yml down     # keeps data
docker compose --project-directory . -f docker/docker-compose.mysql.yml -f docker/docker-compose.monitoring.yml down -v  # wipes data
```

## 5 Results

Saved in `load_test_results/<timestamp>/`:

| File                               | Contents                                                                |
|------------------------------------|-------------------------------------------------------------------------|
| `requirements_check.md`            | Stage-by-stage table with TPS, avg, p95, p99, errors, and ✔/✘ verdict |
| `saturation_curve.md`              | Throughput curve: peak, inflection point, operational capacity          |
| `saturation_curve.csv`             | Curve coordinates for plotting                                          |
| `stage_results.csv`                | Stage summary metrics                                                   |
| `stage_results_by_transaction.csv` | Metrics broken down by transaction type                                 |
| `locust_report.html`               | Locust HTML report                                                      |
| `locust_*.csv`                     | Raw Locust statistics                                                   |
| `worker_*.log`                     | Worker logs                                                             |
| `run_load_test.log`                | Full runner and master process console log                              |

Only the steady-state period of each stage is measured: the measurement window begins only after all clients have
spawned, ensuring ramp-up periods do not distort the numbers.

## 6 PyCharm Run Configurations

Run → Edit Configurations → **+** → Python. In each configuration:
Environment variables → `MYSQL_PASSWORD=parcel_delivery_root`, working directory — project root, interpreter — project's
`.venv`.

| Name              | Script                              | Parameters                          |
|-------------------|-------------------------------------|-------------------------------------|
| Seed — smoke      | `seed_fake_parcel_delivery_data.py` | `--scale 0.1`                       |
| Seed — full       | `seed_fake_parcel_delivery_data.py` | —                                   |
| Load test — smoke | `run_load_test.py`                  | `--workers 8 --minutes-per-stage 2` |
| Load test — full  | `run_load_test.py`                  | `--workers 8`                       |
| Saturation steps  | `run_load_test.py`                  | `--plan saturation --workers 8`     |

## 7 Troubleshooting

| Symptom                                          | Root Cause & Fix                                                                                                                                                                                        |
|--------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `locust` target is DOWN during the test          | Add an inbound rule in Windows Firewall for TCP port 9646                                                                                                                                               |
| `mysql` target is DOWN                           | The MySQL container is still initializing — verify that `docker compose ... ps` reports `healthy`                                                                                                       |
| `Access denied` in scripts                       | Script was executed without `--env-file .env`, or `MYSQL_PASSWORD` is missing from the PyCharm configuration                                                                                            |
| MySQL container crashes with OOM at 5000 clients | Lower `INNODB_BUFFER_POOL_SIZE` in `.env`                                                                                                                                                               |
| Migrating to a native/local MySQL instance       | Set `MYSQLD_EXPORTER_TARGET=host.docker.internal:3306` in `.env`, do not start `docker/docker-compose.mysql.yml`, and execute `sql/mysql_server_settings.sql` & `sql/mysql_monitoring_user.sql` as root |

## 8 Hardware / Test Setup Verification

Ryzen AI 7 350 (16 threads), 31 GB RAM, Windows 11, Docker Desktop 29.6.2, MySQL 9.7.2.  
Full end-to-end run: dataset seeding, `warmup_100` stage at 100 users yielded 4,728 TPS, 0 errors across all 7
transactions, `requirements_check.md` generated, and Grafana ingested both Locust and MySQL metrics.

## 9 License

The project is licensed under the [MIT License](LICENSE.md).