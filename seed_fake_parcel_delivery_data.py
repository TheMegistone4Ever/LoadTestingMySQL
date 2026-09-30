import argparse
import logging
import os
import random
import time
from collections import Counter
from datetime import datetime, timedelta
from itertools import accumulate
from multiprocessing import Pool
from pathlib import Path

import pymysql
from faker import Faker
from faker.providers.address.uk_UA import Provider as UkrainianAddressProvider
from faker.providers.person.uk_UA import Provider as UkrainianPersonProvider

from console_and_file_logging import build_timestamped_log_file_path, configure_logging
from database_connection_settings import DATABASE_NAME, open_database_connection
from parcel_dataset_specification import (
    BUSINESS_SENDERS_SHARE_OF_CUSTOMERS,
    BUSINESS_SENDERS_SHARE_OF_PARCELS,
    DATASET_RANDOM_SEED,
    DEFAULT_DATA_SCALE,
    FINISHED_PARCEL_STATUSES,
    PARCEL_STATUS_SHARES,
    SORTING_HUB_ROUTING_PROBABILITY,
    SORTING_HUBS_COUNT,
    build_phone_number,
    build_tracking_number,
    calculate_dataset_row_counts,
)

SCHEMA_FILE_PATH = Path(__file__).parent / "sql" / "parcel_delivery_schema.sql"
LOGS_DIRECTORY = Path(__file__).with_name("logs")
LOG_FILE_NAME_PREFIX = "seed_fake_parcel_delivery_data"
SCHEMA_PHASE_SEPARATOR = "PHASE 2: AFTER DATA LOAD"
CUSTOMERS_PER_LOAD_TASK = 50_000
PARCELS_PER_LOAD_TASK = 20_000
PROGRESS_REPORT_INTERVAL_SECONDS = 5
MINIMUM_RECOMMENDED_BUFFER_POOL_BYTES = 1024 ** 3

MAJOR_CITIES = (
    ("Київ", "м. Київ"),
    ("Харків", "Харківська область"),
    ("Одеса", "Одеська область"),
    ("Дніпро", "Дніпропетровська область"),
    ("Львів", "Львівська область"),
    ("Запоріжжя", "Запорізька область"),
    ("Вінниця", "Вінницька область"),
    ("Полтава", "Полтавська область"),
    ("Чернігів", "Чернігівська область"),
    ("Івано-Франківськ", "Івано-Франківська область"),
)
CITY_SIZE_SKEW_EXPONENT = 0.9
SORTING_HUB_CITIES_COUNT = 50
PARCEL_LOCKER_SHARE = 0.6

CUSTOMER_REGISTRATION_START = datetime(2015, 1, 1)
EMAIL_OWNERS_SHARE = 0.6
EMAIL_DOMAINS = ("gmail.com", "ukr.net", "i.ua", "meta.ua", "outlook.com")
PARCEL_HISTORY_SECONDS = 3 * 365 * 24 * 3600
IN_PROGRESS_IDLE_SECONDS = 24 * 3600
TARIFF_BASE_YEAR = 2023

UKRAINIAN_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ж": "zh", "з": "z",
    "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ь": "", "ю": "iu", "я": "ia", "'": "", "ʼ": "", "’": "",
}

INSERT_CITIES_SQL = "INSERT INTO cities (id, name, region) VALUES (%s, %s, %s)"
INSERT_BRANCHES_SQL = (
    "INSERT INTO branches (id, city_id, branch_number, branch_type, address) VALUES (%s, %s, %s, %s, %s)"
)
INSERT_CUSTOMERS_SQL = (
    "INSERT INTO customers (id, phone, first_name, last_name, email, registered_at) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
INSERT_PARCELS_SQL = (
    "INSERT INTO parcels (id, tracking_number, sender_id, recipient_id, origin_branch_id, destination_branch_id, "
    "weight_kg, declared_value, delivery_cost, status, created_at, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
INSERT_TRACKING_EVENTS_SQL = (
    "INSERT INTO tracking_events (parcel_id, branch_id, status, event_time) VALUES (%s, %s, %s, %s)"
)

loader_worker_state = {}

logger = logging.getLogger(__name__)


def parse_command_line_arguments():
    """Parse the data scale and the number of loader processes.

    Returns:
        Parsed command line arguments.
    """

    parser = argparse.ArgumentParser(description="Заповнення БД parcel_delivery фейковими даними")
    parser.add_argument("--scale", type=float, default=DEFAULT_DATA_SCALE,
                        help="1.0 ≈ 100 млн рядків (за замовчуванням), 0.1 ≈ 10 млн")
    parser.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) // 2),
                        help="кількість паралельних процесів заливки")
    return parser.parse_args()


def read_schema_phases():
    """Split the schema file into the table creation and the post-load index phases.

    Returns:
        Tuple of SQL scripts (tables, indexes and foreign keys).
    """

    tables_sql, indexes_sql = SCHEMA_FILE_PATH.read_text(encoding="utf-8").split(SCHEMA_PHASE_SEPARATOR)
    return tables_sql, indexes_sql


def split_sql_statements(sql_script):
    """Split an SQL script without procedures into single statements.

    Args:
        sql_script: SQL text with statements separated by semicolons.

    Returns:
        List of statements without comments.
    """

    code_lines = [line for line in sql_script.splitlines() if not line.strip().startswith("--")]
    return [statement.strip() for statement in "\n".join(code_lines).split(";") if statement.strip()]


def disable_binary_logging_for_session(connection):
    """Skip binary logging of the bulk load in this session to halve disk writes.

    Args:
        connection: Open MySQL connection.

    Returns:
        True when binary logging was disabled.
    """

    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION sql_log_bin = 0")
        return True
    except pymysql.err.MySQLError:
        return False


def recreate_database():
    """Drop and create the parcel delivery database and warn about a default-sized buffer pool."""

    with open_database_connection(select_database=False, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS `{DATABASE_NAME}`")
        cursor.execute(f"CREATE DATABASE `{DATABASE_NAME}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        cursor.execute("SELECT @@innodb_buffer_pool_size, VERSION()")
        buffer_pool_bytes, server_version = cursor.fetchone()
    logger.info("MySQL %s: БД `%s` перестворено, buffer pool = %.1f GB",
                server_version, DATABASE_NAME, buffer_pool_bytes / 1024 ** 3)
    if buffer_pool_bytes < MINIMUM_RECOMMENDED_BUFFER_POOL_BYTES:
        logger.warning("  buffer pool замалий — спершу виконай sql/mysql_server_settings.sql під root")


def transliterate_to_latin(ukrainian_text):
    """Transliterate Ukrainian text to lowercase Latin for e-mail addresses.

    Args:
        ukrainian_text: Text in Ukrainian.

    Returns:
        Lowercase Latin transliteration.
    """

    return "".join(UKRAINIAN_TO_LATIN.get(letter, letter if letter.isascii() else "")
                   for letter in ukrainian_text.lower())


def build_customer_name_pools():
    """Collect gendered Ukrainian first and last names with their Latin forms.

    Returns:
        Dict with name tuples per gender and a transliteration map.
    """

    all_names = set(UkrainianPersonProvider.first_names_male + UkrainianPersonProvider.first_names_female
                    + UkrainianPersonProvider.last_names_male + UkrainianPersonProvider.last_names_female)
    return {
        "male": (UkrainianPersonProvider.first_names_male, UkrainianPersonProvider.last_names_male),
        "female": (UkrainianPersonProvider.first_names_female, UkrainianPersonProvider.last_names_female),
        "latin_names": {name: transliterate_to_latin(name) for name in all_names},
    }


def generate_city_rows(random_generator, cities_count):
    """Generate unique (name, region) cities, the largest ones first.

    Args:
        random_generator: Seeded random generator.
        cities_count: Number of cities to generate.

    Returns:
        List of (id, name, region) tuples.
    """

    major_city_names = {city_name for city_name, _ in MAJOR_CITIES}
    candidate_city_names = [name for name in UkrainianAddressProvider.city_names if name not in major_city_names]
    city_pairs = list(MAJOR_CITIES)
    used_city_pairs = set(city_pairs)
    while len(city_pairs) < cities_count:
        city_pair = (random_generator.choice(candidate_city_names),
                     random_generator.choice(UkrainianAddressProvider.region_names))
        if city_pair not in used_city_pairs:
            used_city_pairs.add(city_pair)
            city_pairs.append(city_pair)
    return [(city_id, name, region) for city_id, (name, region) in enumerate(city_pairs, start=1)]


def generate_branch_rows(random_generator, faker, cities_count, branches_count):
    """Generate branches, parcel lockers and sorting hubs with a Zipf-skewed spread across cities.

    Args:
        random_generator: Seeded random generator.
        faker: Ukrainian Faker instance for street names.
        cities_count: Number of cities.
        branches_count: Number of branches to generate.

    Returns:
        List of (id, city_id, branch_number, branch_type, address) tuples.
    """

    city_size_weights = [1.0 / (city_rank + 1) ** CITY_SIZE_SKEW_EXPONENT for city_rank in range(cities_count)]
    branches_per_city = [1] * cities_count
    for city_index in random_generator.choices(range(cities_count), weights=city_size_weights,
                                               k=branches_count - cities_count):
        branches_per_city[city_index] += 1

    branch_rows = []
    for city_index, city_branches_count in enumerate(branches_per_city):
        for branch_number in range(1, city_branches_count + 1):
            branch_type = "parcel_locker" if random_generator.random() < PARCEL_LOCKER_SHARE else "branch"
            address = f"вул. {faker.street_name()}, {random_generator.randint(1, 250)}"
            branch_rows.append([len(branch_rows) + 1, city_index + 1, branch_number, branch_type, address])

    hub_candidate_indexes = [index for index, row in enumerate(branch_rows) if row[1] <= SORTING_HUB_CITIES_COUNT]
    for hub_index in random_generator.sample(hub_candidate_indexes, SORTING_HUBS_COUNT):
        branch_rows[hub_index][3] = "sorting_hub"
    return [tuple(row) for row in branch_rows]


def build_loader_reference_data(branch_rows, row_counts):
    """Prepare lookup data every loader process needs to generate consistent foreign keys.

    Args:
        branch_rows: Generated branch rows.
        row_counts: Target row counts.

    Returns:
        Dict with branch ids, hub ids, branch cities, customer bounds and name pools.
    """

    return {
        "delivery_branch_ids": [row[0] for row in branch_rows if row[3] != "sorting_hub"],
        "sorting_hub_ids": [row[0] for row in branch_rows if row[3] == "sorting_hub"],
        "branch_city_ids": [0] + [row[1] for row in branch_rows],
        "customers_count": row_counts.customers,
        "business_senders_last_id": max(1, int(row_counts.customers * BUSINESS_SENDERS_SHARE_OF_CUSTOMERS)),
        "name_pools": build_customer_name_pools(),
        "generation_moment": datetime.now().replace(microsecond=0),
    }


def generate_customer_rows(first_customer_id, last_customer_id, random_generator, reference_data):
    """Generate customers with gender-consistent names, unique phones and optional e-mails.

    Args:
        first_customer_id: First customer id of the range.
        last_customer_id: Last customer id of the range (inclusive).
        random_generator: Seeded random generator.
        reference_data: Loader reference data.

    Returns:
        List of customer row tuples.
    """

    name_pools = reference_data["name_pools"]
    latin_names = name_pools["latin_names"]
    registration_span_seconds = int((reference_data["generation_moment"] - CUSTOMER_REGISTRATION_START).total_seconds())
    customer_rows = []
    for customer_id in range(first_customer_id, last_customer_id + 1):
        first_names, last_names = name_pools["female" if random_generator.random() < 0.5 else "male"]
        first_name = random_generator.choice(first_names)
        last_name = random_generator.choice(last_names)
        email = None
        if random_generator.random() < EMAIL_OWNERS_SHARE:
            email = (f"{latin_names[first_name]}.{latin_names[last_name]}{customer_id}"
                     f"@{random_generator.choice(EMAIL_DOMAINS)}")
        registered_at = CUSTOMER_REGISTRATION_START + timedelta(
            seconds=random_generator.randrange(registration_span_seconds))
        customer_rows.append((customer_id, build_phone_number(customer_id), first_name, last_name, email,
                              registered_at))
    return customer_rows


def build_tracking_route(final_status, origin_branch_id, destination_branch_id, sorting_hub_id, random_generator):
    """Build the scan events a parcel passed through until its final status.

    Args:
        final_status: Current parcel status.
        origin_branch_id: Sender branch id.
        destination_branch_id: Recipient branch id.
        sorting_hub_id: Sorting hub id or None for a direct route.
        random_generator: Seeded random generator.

    Returns:
        List of (status, branch_id, seconds_after_creation) tuples.
    """

    progress_rank = {"created": 0, "accepted": 1, "in_transit": 2, "arrived": 3}.get(final_status, 4)
    elapsed_seconds = 0
    route = [("created", origin_branch_id, 0)]
    if progress_rank >= 1:
        elapsed_seconds += random_generator.randint(1_800, 86_400)
        route.append(("accepted", origin_branch_id, elapsed_seconds))
    if progress_rank >= 2:
        elapsed_seconds += random_generator.randint(3_600, 43_200)
        route.append(("in_transit", origin_branch_id, elapsed_seconds))
        if sorting_hub_id is not None:
            elapsed_seconds += random_generator.randint(14_400, 64_800)
            route.append(("in_transit", sorting_hub_id, elapsed_seconds))
    if progress_rank >= 3:
        elapsed_seconds += random_generator.randint(14_400, 108_000)
        route.append(("arrived", destination_branch_id, elapsed_seconds))
    if final_status == "delivered":
        elapsed_seconds += random_generator.randint(1_800, 345_600)
        route.append(("delivered", destination_branch_id, elapsed_seconds))
    elif final_status == "returned":
        elapsed_seconds += random_generator.randint(432_000, 604_800)
        route.append(("returned", destination_branch_id, elapsed_seconds))
    return route


def generate_parcel_and_event_rows(first_parcel_id, last_parcel_id, random_generator, reference_data):
    """Generate parcels together with their consistent tracking event history.

    Args:
        first_parcel_id: First parcel id of the range.
        last_parcel_id: Last parcel id of the range (inclusive).
        random_generator: Seeded random generator.
        reference_data: Loader reference data.

    Returns:
        Tuple of parcel rows and tracking event rows.
    """

    delivery_branch_ids = reference_data["delivery_branch_ids"]
    sorting_hub_ids = reference_data["sorting_hub_ids"]
    branch_city_ids = reference_data["branch_city_ids"]
    customers_count = reference_data["customers_count"]
    business_senders_last_id = reference_data["business_senders_last_id"]
    generation_moment = reference_data["generation_moment"]
    parcel_statuses = list(PARCEL_STATUS_SHARES)
    cumulative_status_shares = list(accumulate(PARCEL_STATUS_SHARES.values()))

    parcel_rows, event_rows = [], []
    for parcel_id in range(first_parcel_id, last_parcel_id + 1):
        final_status = random_generator.choices(parcel_statuses, cum_weights=cumulative_status_shares)[0]
        if random_generator.random() < BUSINESS_SENDERS_SHARE_OF_PARCELS:
            sender_id = random_generator.randint(1, business_senders_last_id)
        else:
            sender_id = random_generator.randint(1, customers_count)
        recipient_id = random_generator.randint(1, customers_count)
        while recipient_id == sender_id:
            recipient_id = random_generator.randint(1, customers_count)
        origin_branch_id = random_generator.choice(delivery_branch_ids)
        destination_branch_id = random_generator.choice(delivery_branch_ids)
        while destination_branch_id == origin_branch_id:
            destination_branch_id = random_generator.choice(delivery_branch_ids)
        sorting_hub_id = (random_generator.choice(sorting_hub_ids)
                          if random_generator.random() < SORTING_HUB_ROUTING_PROBABILITY else None)

        route = build_tracking_route(final_status, origin_branch_id, destination_branch_id, sorting_hub_id,
                                     random_generator)
        route_duration_seconds = route[-1][2]
        idle_seconds_limit = (PARCEL_HISTORY_SECONDS if final_status in FINISHED_PARCEL_STATUSES
                              else IN_PROGRESS_IDLE_SECONDS)
        created_at = generation_moment - timedelta(
            seconds=route_duration_seconds + random_generator.randrange(idle_seconds_limit))

        weight_kg = round(min(30.0, max(0.1, random_generator.lognormvariate(0.3, 0.9))), 2)
        declared_value = round(min(100_000.0, max(200.0, random_generator.lognormvariate(6.7, 1.0))), 2)
        is_intercity = branch_city_ids[origin_branch_id] != branch_city_ids[destination_branch_id]
        base_tariff = 55 + 5 * (created_at.year - TARIFF_BASE_YEAR) + (25 if is_intercity else 0)
        delivery_cost = round(base_tariff + 12 * weight_kg + 0.005 * declared_value, 2)

        parcel_rows.append((
            parcel_id, build_tracking_number(parcel_id), sender_id, recipient_id, origin_branch_id,
            destination_branch_id, weight_kg, declared_value, delivery_cost, final_status, created_at,
            created_at + timedelta(seconds=route_duration_seconds),
        ))
        event_rows.extend((parcel_id, branch_id, status, created_at + timedelta(seconds=seconds_after_creation))
                          for status, branch_id, seconds_after_creation in route)
    return parcel_rows, event_rows


def initialize_loader_worker(reference_data):
    """Open a MySQL session for one loader process and keep reference data in it.

    Args:
        reference_data: Loader reference data.
    """

    connection = open_database_connection()
    disable_binary_logging_for_session(connection)
    loader_worker_state.update(connection=connection, reference_data=reference_data)


def load_customer_range(id_range):
    """Generate and insert one range of customers.

    Args:
        id_range: Tuple (first_customer_id, last_customer_id).

    Returns:
        Counter of inserted rows per table.
    """

    first_customer_id, last_customer_id = id_range
    random_generator = random.Random(DATASET_RANDOM_SEED * 1_000_003 + first_customer_id)
    customer_rows = generate_customer_rows(first_customer_id, last_customer_id, random_generator,
                                           loader_worker_state["reference_data"])
    connection = loader_worker_state["connection"]
    with connection.cursor() as cursor:
        cursor.executemany(INSERT_CUSTOMERS_SQL, customer_rows)
    connection.commit()
    return Counter(customers=len(customer_rows))


def load_parcel_range(id_range):
    """Generate and insert one range of parcels with their tracking events in one transaction.

    Args:
        id_range: Tuple (first_parcel_id, last_parcel_id).

    Returns:
        Counter of inserted rows per table.
    """

    first_parcel_id, last_parcel_id = id_range
    random_generator = random.Random(DATASET_RANDOM_SEED * 2_000_003 + first_parcel_id)
    parcel_rows, event_rows = generate_parcel_and_event_rows(first_parcel_id, last_parcel_id, random_generator,
                                                             loader_worker_state["reference_data"])
    connection = loader_worker_state["connection"]
    with connection.cursor() as cursor:
        cursor.executemany(INSERT_PARCELS_SQL, parcel_rows)
        cursor.executemany(INSERT_TRACKING_EVENTS_SQL, event_rows)
    connection.commit()
    return Counter(parcels=len(parcel_rows), tracking_events=len(event_rows))


def split_id_range(rows_count, rows_per_task):
    """Split ids 1..rows_count into consecutive inclusive ranges.

    Args:
        rows_count: Total number of ids.
        rows_per_task: Maximum ids per range.

    Returns:
        List of (first_id, last_id) tuples.
    """

    return [(first_id, min(first_id + rows_per_task - 1, rows_count))
            for first_id in range(1, rows_count + 1, rows_per_task)]


def run_parallel_load(loader_pool, load_task, id_ranges, expected_rows, stage_label):
    """Run load tasks in the process pool and log throughput with an ETA.

    Args:
        loader_pool: Multiprocessing pool of loader workers.
        load_task: Task function that returns inserted row counters.
        id_ranges: Id ranges to distribute across workers.
        expected_rows: Expected rows of the stage for the progress estimate.
        stage_label: Stage name for the log.

    Returns:
        Counter of inserted rows per table.
    """

    inserted_rows = Counter()
    stage_started_at = last_report_at = time.perf_counter()
    for task_rows in loader_pool.imap_unordered(load_task, id_ranges):
        inserted_rows += task_rows
        current_time = time.perf_counter()
        if current_time - last_report_at >= PROGRESS_REPORT_INTERVAL_SECONDS:
            last_report_at = current_time
            stage_rows = sum(inserted_rows.values())
            rows_per_second = stage_rows / (current_time - stage_started_at)
            remaining_minutes = max(0, expected_rows - stage_rows) / rows_per_second / 60
            logger.info("  %s: %s / ~%s рядків (%.0f%%), %s рядків/с, залишилось ≈%.1f хв",
                        stage_label, f"{stage_rows:,}", f"{expected_rows:,}",
                        100 * stage_rows / expected_rows, f"{rows_per_second:,.0f}", remaining_minutes)
    stage_seconds = time.perf_counter() - stage_started_at
    logger.info("  %s: %s рядків за %.1f хв", stage_label, f"{sum(inserted_rows.values()):,}", stage_seconds / 60)
    return inserted_rows


def build_secondary_indexes_and_foreign_keys(indexes_sql, index_build_threads):
    """Build unique keys, indexes and foreign keys after the bulk load.

    Args:
        indexes_sql: SQL of the post-load schema phase.
        index_build_threads: Threads for InnoDB sorted index builds.
    """

    with open_database_connection(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("SET SESSION foreign_key_checks = 0")
        for tuning_statement in (f"SET SESSION innodb_ddl_threads = {index_build_threads}",
                                 "SET SESSION innodb_ddl_buffer_size = 256 * 1024 * 1024"):
            try:
                cursor.execute(tuning_statement)
            except pymysql.err.MySQLError:
                pass
        for alter_statement in split_sql_statements(indexes_sql):
            table_name = alter_statement.split()[2]
            statement_started_at = time.perf_counter()
            cursor.execute(alter_statement)
            logger.info("  %s: індекси та FK за %.0f с", table_name, time.perf_counter() - statement_started_at)
        cursor.execute("SET SESSION foreign_key_checks = 1")
        cursor.execute("ANALYZE TABLE cities, branches, customers, parcels, tracking_events")
        cursor.fetchall()


def log_database_size_report(inserted_rows):
    """Log inserted rows and on-disk size of every table.

    Args:
        inserted_rows: Counter of inserted rows per table.
    """

    with open_database_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SET SESSION information_schema_stats_expiry = 0")
        cursor.execute(
            "SELECT table_name, data_length, index_length FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY data_length + index_length DESC", (DATABASE_NAME,))
        table_sizes = cursor.fetchall()
    logger.info(f"{'таблиця':<17}{'рядків':>14}{'дані, MB':>11}{'індекси, MB':>13}")
    for table_name, data_bytes, index_bytes in table_sizes:
        logger.info(f"{table_name:<17}{inserted_rows[table_name]:>14,}{data_bytes / 1024 ** 2:>11,.0f}"
                    f"{index_bytes / 1024 ** 2:>13,.0f}")
    total_bytes = sum(data_bytes + index_bytes for _, data_bytes, index_bytes in table_sizes)
    logger.info(f"{'РАЗОМ':<17}{sum(inserted_rows.values()):>14,}   {total_bytes / 1024 ** 3:,.1f} GB на диску")


def main():
    """Recreate the database, load the fake dataset and build indexes."""

    configure_logging(build_timestamped_log_file_path(LOGS_DIRECTORY, LOG_FILE_NAME_PREFIX))
    arguments = parse_command_line_arguments()
    row_counts = calculate_dataset_row_counts(arguments.scale)
    logger.info("План (scale=%s): ~%s рядків, %s процесів заливки",
                arguments.scale, f"{row_counts.expected_total_rows:,}", arguments.workers)
    script_started_at = time.perf_counter()
    tables_sql, indexes_sql = read_schema_phases()
    recreate_database()

    random_generator = random.Random(DATASET_RANDOM_SEED)
    Faker.seed(DATASET_RANDOM_SEED)
    faker = Faker("uk_UA")
    city_rows = generate_city_rows(random_generator, row_counts.cities)
    branch_rows = generate_branch_rows(random_generator, faker, row_counts.cities, row_counts.branches)
    with open_database_connection() as connection:
        binary_logging_disabled = disable_binary_logging_for_session(connection)
        with connection.cursor() as cursor:
            for create_statement in split_sql_statements(tables_sql):
                cursor.execute(create_statement)
            cursor.executemany(INSERT_CITIES_SQL, city_rows)
            cursor.executemany(INSERT_BRANCHES_SQL, branch_rows)
        connection.commit()
    logger.info("cities: %s, branches: %s (binlog для заливки %s)",
                f"{len(city_rows):,}", f"{len(branch_rows):,}",
                "вимкнено" if binary_logging_disabled else "НЕ вимкнено — немає прав")

    inserted_rows = Counter(cities=len(city_rows), branches=len(branch_rows))
    reference_data = build_loader_reference_data(branch_rows, row_counts)
    with Pool(arguments.workers, initializer=initialize_loader_worker, initargs=(reference_data,)) as loader_pool:
        inserted_rows += run_parallel_load(loader_pool, load_customer_range,
                                           split_id_range(row_counts.customers, CUSTOMERS_PER_LOAD_TASK),
                                           row_counts.customers, "customers")
        inserted_rows += run_parallel_load(loader_pool, load_parcel_range,
                                           split_id_range(row_counts.parcels, PARCELS_PER_LOAD_TASK),
                                           row_counts.parcels + row_counts.expected_tracking_events,
                                           "parcels + tracking_events")

    logger.info("Будую індекси та FK (сортуванням, після заливки)...")
    build_secondary_indexes_and_foreign_keys(indexes_sql, index_build_threads=max(4, arguments.workers))
    log_database_size_report(inserted_rows)
    logger.info("Готово за %.1f хв", (time.perf_counter() - script_started_at) / 60)


if __name__ == "__main__":
    main()
