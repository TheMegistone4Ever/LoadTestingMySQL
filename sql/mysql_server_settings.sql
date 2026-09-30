SET
PERSIST innodb_buffer_pool_size = 8 * 1024 * 1024 * 1024;
SET
PERSIST innodb_redo_log_capacity = 4 * 1024 * 1024 * 1024;
SET
PERSIST max_connections = 6000;

SELECT @@innodb_buffer_pool_size / POW(1024, 3)  AS buffer_pool_gb,
       @@innodb_redo_log_capacity / POW(1024, 3) AS redo_log_gb,
       @@max_connections                         AS max_connections;
