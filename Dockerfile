app = "gotovo-bot"
primary_region = "waw"

[env]
  TESS_LANGS = "bel+rus+eng"
  TESS_CONFIG = "--oem 3 --psm 6 -c preserve_interword_spaces=1"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "off"   # не гасим
  auto_start_machines = true
  min_machines_running = 1     # если «не останавливать», держим хотя бы 1

  [[http_service.checks]]
    interval = "15s"
    timeout = "10s"
    grace_period = "5s"
    method = "GET"
    path = "/"

[[vm]]
  cpu_kind = "shared"
  cpus = 1
  memory = "1gb"


