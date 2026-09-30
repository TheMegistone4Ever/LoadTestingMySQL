from dataclasses import dataclass

DEFAULT_DATA_SCALE = 1.0
DATASET_RANDOM_SEED = 42

CITIES_COUNT = 1_200
BRANCHES_COUNT = 25_000
SORTING_HUBS_COUNT = 150
CUSTOMERS_AT_FULL_SCALE = 4_000_000
PARCELS_AT_FULL_SCALE = 16_000_000

PARCEL_STATUS_SHARES = {
    "created": 0.04,
    "accepted": 0.05,
    "in_transit": 0.10,
    "arrived": 0.08,
    "delivered": 0.70,
    "returned": 0.03,
}
FINISHED_PARCEL_STATUSES = ("delivered", "returned")
TRACKING_EVENTS_PER_FINAL_STATUS = {
    "created": 1,
    "accepted": 2,
    "in_transit": 3,
    "arrived": 4,
    "delivered": 5,
    "returned": 5,
}
SORTING_HUB_ROUTING_PROBABILITY = 0.6
BUSINESS_SENDERS_SHARE_OF_CUSTOMERS = 0.01
BUSINESS_SENDERS_SHARE_OF_PARCELS = 0.35

SEEDED_TRACKING_NUMBER_PREFIX = "20"
LOAD_TEST_TRACKING_NUMBER_PREFIX = "59"
TRACKING_NUMBER_SPACE = 10 ** 12
TRACKING_NUMBER_MULTIPLIER = 738_219_473_191
TRACKING_NUMBER_OFFSET = 271_828_182_845

MOBILE_OPERATOR_CODES = ("50", "63", "66", "67", "68", "73", "93", "95", "96", "97", "98", "99")
PHONE_SUBSCRIBER_SPACE = 10 ** 7
PHONE_NUMBER_SPACE = len(MOBILE_OPERATOR_CODES) * PHONE_SUBSCRIBER_SPACE
PHONE_NUMBER_MULTIPLIER = 97_654_321
PHONE_NUMBER_OFFSET = 1_234_567


@dataclass(frozen=True)
class DatasetRowCounts:
    """Target row counts of every table."""

    cities: int
    branches: int
    customers: int
    parcels: int
    expected_tracking_events: int

    @property
    def expected_total_rows(self):
        """Sum the expected rows of all tables.

        Returns:
            Expected total row count.
        """

        return self.cities + self.branches + self.customers + self.parcels + self.expected_tracking_events


def calculate_expected_events_per_parcel():
    """Calculate the mean number of tracking events per parcel from the status shares.

    Returns:
        Mean tracking events per parcel.
    """

    return sum(
        share * (TRACKING_EVENTS_PER_FINAL_STATUS[status]
                 + (SORTING_HUB_ROUTING_PROBABILITY if status in ("in_transit", "arrived", "delivered", "returned")
                    else 0.0))
        for status, share in PARCEL_STATUS_SHARES.items()
    )


def calculate_dataset_row_counts(data_scale):
    """Calculate table row counts for a data scale.

    Args:
        data_scale: Multiplier of customers and parcels volume (1.0 ≈ 100 million rows).

    Returns:
        Row counts of every table.
    """

    customers_count = max(1_000, int(CUSTOMERS_AT_FULL_SCALE * data_scale))
    parcels_count = max(1_000, int(PARCELS_AT_FULL_SCALE * data_scale))
    if customers_count > PHONE_NUMBER_SPACE:
        raise ValueError("Scale is too large for the unique phone number space")
    return DatasetRowCounts(
        cities=CITIES_COUNT,
        branches=BRANCHES_COUNT,
        customers=customers_count,
        parcels=parcels_count,
        expected_tracking_events=round(parcels_count * calculate_expected_events_per_parcel()),
    )


def build_tracking_number(parcel_id):
    """Map a seeded parcel id to its unique 14-digit tracking number.

    Args:
        parcel_id: Parcel primary key.

    Returns:
        Tracking number (ТТН) string.
    """

    scrambled_number = (parcel_id * TRACKING_NUMBER_MULTIPLIER + TRACKING_NUMBER_OFFSET) % TRACKING_NUMBER_SPACE
    return f"{SEEDED_TRACKING_NUMBER_PREFIX}{scrambled_number:012d}"


def build_phone_number(customer_id):
    """Map a customer id to a unique Ukrainian mobile phone number.
    Faker does not generate unique ukrainian phone numbers.

    Args:
        customer_id: Customer primary key.

    Returns:
        Phone number in +380XXXXXXXXX format.
    """

    scrambled_number = (customer_id * PHONE_NUMBER_MULTIPLIER + PHONE_NUMBER_OFFSET) % PHONE_NUMBER_SPACE
    operator_code = MOBILE_OPERATOR_CODES[scrambled_number // PHONE_SUBSCRIBER_SPACE]
    return f"+380{operator_code}{scrambled_number % PHONE_SUBSCRIBER_SPACE:07d}"
