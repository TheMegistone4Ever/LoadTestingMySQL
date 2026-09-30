-- PHASE 1: BEFORE DATA LOAD

CREATE TABLE cities
(
    id     INT          NOT NULL AUTO_INCREMENT,
    name   VARCHAR(100) NOT NULL,
    region VARCHAR(100) NOT NULL,
    PRIMARY KEY (id)
) ENGINE = InnoDB;

CREATE TABLE branches
(
    id            INT          NOT NULL AUTO_INCREMENT,
    city_id       INT          NOT NULL,
    branch_number SMALLINT     NOT NULL,
    branch_type   ENUM ('branch', 'parcel_locker', 'sorting_hub') NOT NULL,
    address       VARCHAR(255) NOT NULL,
    PRIMARY KEY (id)
) ENGINE = InnoDB;

CREATE TABLE customers
(
    id            BIGINT                          NOT NULL AUTO_INCREMENT,
    phone         VARCHAR(20) CHARACTER SET ascii NOT NULL,
    first_name    VARCHAR(50)                     NOT NULL,
    last_name     VARCHAR(50)                     NOT NULL,
    email         VARCHAR(100) NULL,
    registered_at DATETIME                        NOT NULL,
    PRIMARY KEY (id)
) ENGINE = InnoDB;

CREATE TABLE parcels
(
    id                    BIGINT                       NOT NULL AUTO_INCREMENT,
    tracking_number       CHAR(14) CHARACTER SET ascii NOT NULL,
    sender_id             BIGINT                       NOT NULL,
    recipient_id          BIGINT                       NOT NULL,
    origin_branch_id      INT                          NOT NULL,
    destination_branch_id INT                          NOT NULL,
    weight_kg             DECIMAL(6, 2)                NOT NULL,
    declared_value        DECIMAL(10, 2)               NOT NULL,
    delivery_cost         DECIMAL(8, 2)                NOT NULL,
    status                ENUM ('created', 'accepted', 'in_transit', 'arrived', 'delivered', 'returned') NOT NULL,
    created_at            DATETIME                     NOT NULL,
    updated_at            DATETIME                     NOT NULL,
    PRIMARY KEY (id)
) ENGINE = InnoDB;

CREATE TABLE tracking_events
(
    id         BIGINT   NOT NULL AUTO_INCREMENT,
    parcel_id  BIGINT   NOT NULL,
    branch_id  INT      NOT NULL,
    status     ENUM ('created', 'accepted', 'in_transit', 'arrived', 'delivered', 'returned') NOT NULL,
    event_time DATETIME NOT NULL,
    PRIMARY KEY (id)
) ENGINE = InnoDB;

-- PHASE 2: AFTER DATA LOAD

ALTER TABLE cities
    ADD CONSTRAINT uq_cities_name_region UNIQUE (name, region);

ALTER TABLE branches
    ADD CONSTRAINT uq_branches_city_number UNIQUE (city_id, branch_number),
    ADD CONSTRAINT fk_branches_city FOREIGN KEY (city_id) REFERENCES cities (id);

ALTER TABLE customers
    ADD CONSTRAINT uq_customers_phone UNIQUE (phone);

ALTER TABLE parcels
    ADD CONSTRAINT uq_parcels_tracking_number UNIQUE (tracking_number),
    ADD INDEX ix_parcels_sender_created (sender_id, created_at),
    ADD INDEX ix_parcels_recipient_created (recipient_id, created_at),
    ADD INDEX ix_parcels_destination_status (destination_branch_id, status),
    ADD INDEX ix_parcels_origin_branch (origin_branch_id),
    ADD CONSTRAINT fk_parcels_sender FOREIGN KEY (sender_id) REFERENCES customers (id),
    ADD CONSTRAINT fk_parcels_recipient FOREIGN KEY (recipient_id) REFERENCES customers (id),
    ADD CONSTRAINT fk_parcels_origin_branch FOREIGN KEY (origin_branch_id) REFERENCES branches (id),
    ADD CONSTRAINT fk_parcels_destination_branch FOREIGN KEY (destination_branch_id) REFERENCES branches (id);

ALTER TABLE tracking_events
    ADD INDEX ix_tracking_events_parcel_time (parcel_id, event_time),
    ADD INDEX ix_tracking_events_branch (branch_id),
    ADD CONSTRAINT fk_tracking_events_parcel FOREIGN KEY (parcel_id) REFERENCES parcels (id),
    ADD CONSTRAINT fk_tracking_events_branch FOREIGN KEY (branch_id) REFERENCES branches (id);
