from dataclasses import dataclass

SPAWN_RATE_CLIENTS_PER_SECOND = 50
RAMP_GRACE_SECONDS = 30

TRACK_PARCEL_TRANSACTION = "T1 трекінг за ТТН"
TRACKING_HISTORY_TRANSACTION = "T2 історія переміщень"
RECIPIENT_PARCELS_TRANSACTION = "T3 посилки одержувача"
BRANCH_PARCELS_TRANSACTION = "T4 посилки у відділенні"
SCAN_EVENT_TRANSACTION = "T5 сканування"
CREATE_PARCEL_TRANSACTION = "T6 нова посилка"
REDIRECT_PARCEL_TRANSACTION = "T7 переадресація"
DATABASE_CONNECTION_FAILURE_NAME = "підключення до MySQL"
AGGREGATED_TRANSACTION_NAME = "Aggregated"

FINAL_LOAD_STAGE_KIND = "load_final"


@dataclass(frozen=True)
class LoadTestStage:
    """One stage of the load plan."""

    name: str
    concurrent_clients: int
    steady_minutes: float
    kind: str
    description: str


@dataclass(frozen=True)
class LoadStageRequirements:
    """Acceptance thresholds of the working load stage."""

    minimum_transactions_per_second: float
    maximum_average_milliseconds: float
    maximum_percentile_95_milliseconds: float
    maximum_percentile_99_milliseconds: float
    maximum_error_percent: float


@dataclass(frozen=True)
class StressStageRequirements:
    """Acceptance thresholds of the stress stages."""

    minimum_share_of_load_stage_tps: float
    maximum_percentile_95_milliseconds: float
    maximum_error_percent: float
    maximum_connection_failures: int


@dataclass(frozen=True)
class RecoveryStageRequirements:
    """Acceptance threshold of the recovery stage after the stress ladder."""

    minimum_share_of_load_stage_tps: float


LOAD_TEST_STAGES = (
    LoadTestStage("warmup_100", 100, 5.0, "warmup", "прогрів buffer pool, у вимоги не входить"),
    LoadTestStage("load_500", 500, 30.0, "load", "робоче навантаження"),
    LoadTestStage("stress_1000", 1000, 30.0, "stress", "перевищення норми у 2 рази"),
    LoadTestStage("stress_2000", 2000, 30.0, "stress", "перевищення норми у 4 рази"),
    LoadTestStage("stress_5000", 5000, 30.0, "stress", "перевищення норми у 10 разів"),
    LoadTestStage("recovery_500", 500, 5.0, "recovery", "відновлення після стресу"),
    LoadTestStage("load_500_final", 500, 30.0, FINAL_LOAD_STAGE_KIND,
                  "повторна базова точка: те саме навантаження на вирослій за прогін БД"),
)

LOAD_STAGE_REQUIREMENTS = LoadStageRequirements(
    minimum_transactions_per_second=3000,
    maximum_average_milliseconds=150,
    maximum_percentile_95_milliseconds=400,
    maximum_percentile_99_milliseconds=1000,
    maximum_error_percent=0.1,
)
STRESS_STAGE_REQUIREMENTS = StressStageRequirements(
    minimum_share_of_load_stage_tps=0.70,
    maximum_percentile_95_milliseconds=5000,
    maximum_error_percent=1.0,
    maximum_connection_failures=0,
)
RECOVERY_STAGE_REQUIREMENTS = RecoveryStageRequirements(minimum_share_of_load_stage_tps=0.90)

SATURATION_LADDER_STAGES = (
    LoadTestStage("warmup_100", 100, 2.0, "warmup", "прогрів buffer pool, у криву не входить"),
    LoadTestStage("scale_100", 100, 3.0, "scale", "висхідна гілка кривої, вимог немає"),
    LoadTestStage("scale_250", 250, 3.0, "scale", "висхідна гілка кривої, вимог немає"),
    LoadTestStage("load_500", 500, 10.0, "load", "базова точка кривої, від неї рахуються частки"),
    LoadTestStage("stress_1000", 1000, 5.0, "stress", "2× норми, контрольна точка з повного прогону"),
    LoadTestStage("stress_1500", 1500, 5.0, "stress", "згущення сходів у зоні перегину"),
    LoadTestStage("stress_2000", 2000, 5.0, "stress", "4× норми, остання точка з ✔ у повному прогоні"),
    LoadTestStage("stress_2500", 2500, 5.0, "stress", "згущення сходів у зоні перегину"),
    LoadTestStage("stress_3000", 3000, 5.0, "stress", "згущення сходів у зоні перегину"),
    LoadTestStage("stress_3500", 3500, 5.0, "stress", "згущення сходів у зоні перегину"),
    LoadTestStage("stress_4000", 4000, 5.0, "stress", "згущення сходів у зоні перегину"),
    LoadTestStage("stress_4500", 4500, 5.0, "stress", "згущення сходів у зоні перегину"),
    LoadTestStage("stress_5000", 5000, 5.0, "stress", "10× норми, контрольна точка з повного прогону"),
    LoadTestStage("recovery_500", 500, 10.0, "recovery", "відновлення після сходів"),
    LoadTestStage("load_500_final", 500, 10.0, FINAL_LOAD_STAGE_KIND,
                  "повторна базова точка: те саме навантаження на вирослій за прогін БД"),
)

REQUIREMENTS_PLAN_NAME = "requirements"
SATURATION_PLAN_NAME = "saturation"
DEFAULT_PLAN_NAME = REQUIREMENTS_PLAN_NAME
LOAD_TEST_PLANS = {
    REQUIREMENTS_PLAN_NAME: LOAD_TEST_STAGES,
    SATURATION_PLAN_NAME: SATURATION_LADDER_STAGES,
}


@dataclass(frozen=True)
class SaturationAnalysisSettings:
    """Rules that turn the measured ladder into a saturation verdict."""

    knee_drop_share: float


SATURATION_ANALYSIS_SETTINGS = SaturationAnalysisSettings(knee_drop_share=0.05)
