# Simulated TLS certs — NOT real keys

Any `redis.crt` / `redis.key` in this directory are **self-signed, throwaway
certificates for the local simulated HEX beamline only**. They are:

- **ephemeral** — generated fresh by [`../../scripts/up.sh`](../../scripts/up.sh) each time the sim starts
  and **discarded by [`../../scripts/down.sh`](../../scripts/down.sh)** when it stops, so a cloner never needs
  a committed copy (just run `up.sh`);
- **not** real NSLS-II / ACME credentials — they carry no trust anywhere and secure nothing real;
- **git-ignored** (`*.crt` / `*.key`) so they are never committed;
- marked **`SIMULATED - NOT A REAL KEY`** in the certificate subject — verify with:

  ```bash
  openssl x509 -in redis.crt -noout -subject
  # subject=O=NSLS-II HEX beamline SIMULATION (hxm_program),
  #         OU=SIMULATED - NOT A REAL KEY - self-signed throwaway,
  #         CN=hexsim-redis.simulated
  ```

**Never place real key material here.** The real HEX Redis (`xf27id1-hex-redis1`)
uses ACME certificates + a vaulted password via the `redis6` ansible role — none
of which is copied into this repository.
