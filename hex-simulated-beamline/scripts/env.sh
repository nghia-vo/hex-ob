# Environment for the simulated HEX beamline. SOURCE this before running the
# blackhole IOC or a bluesky session against the local stack:
#     source scripts/env.sh
#
# Channel Access: talk only to the local sim servers on 127.0.0.1. Two caproto
# servers run on separate CA ports: sim_ioc (detectors + blackhole) on :5064 and
# the dedicated motor IOC on :5075. List both so clients search both.
export EPICS_CA_AUTO_ADDR_LIST=NO
# :5064 sim_ioc (typed sims + blackhole) · :5075 motor IOC ·
# :5085 Kinetix AD IOC (frame tier) · :5095 panda-ioc CA ·
# :5105 Phantom AD IOC (real ADPhantom vs sim_camera)
export EPICS_CA_ADDR_LIST="127.0.0.1:5064 127.0.0.1:5075 127.0.0.1:5085 127.0.0.1:5095 127.0.0.1:5105"

# Resolve this script's dir so the secret/cert paths are absolute.
_HEXSIM_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Local service endpoints (docker-compose maps these to 127.0.0.1).
export REDIS_HOST=127.0.0.1
# Secure Redis: TLS on 6380 (matches the new NSLS-II machines). open_redis_client
# reads the password from REDIS_SECRET_FILE (empty = no auth) and trusts the
# self-signed server cert via SSL_CERT_FILE.
export REDIS_SECRET_FILE="${_HEXSIM_HERE}/../configs/redis.secret"
export SSL_CERT_FILE="${_HEXSIM_HERE}/../compose/certs/redis.crt"
export REQUESTS_CA_BUNDLE="${SSL_CERT_FILE}"
export MONGO_HOST=127.0.0.1
export KAFKA_BOOTSTRAP=127.0.0.1:9092
export BLUESKY_KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092

# Tiled — local server + api key (matches docker-compose `--api-key secret`).
export TILED_URI=http://127.0.0.1:8000
export TILED_API_KEY=secret
export TILED_BLUESKY_WRITING_API_KEY_HEX=secret
export TILED_BLUESKY_WRITING_API_KEY=secret

# Kept for parity with the CI harness / nslsii mongo hooks.
export MONGO_USER_HEX=test
export MONGO_PASSWORD_HEX=test
export BLUESKY_KAFKA_PASSWORD=test
