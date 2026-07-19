# Tachikoma Gateway

The ESP32 client posts JSON to `/v1/chat`; provider credentials stay on this
separate process. The default `mock` provider is offline and requires
`ALLOW_INSECURE_DEV=1` when no device token is configured.

```powershell
$env:AI_PROVIDER='mock'
$env:ALLOW_INSECURE_DEV='1'
python gateway/server.py
curl.exe http://127.0.0.1:8080/health
curl.exe -H 'Content-Type: application/json' -d '{"device_id":"dev","session_id":"s1","request_id":"r1","text":"こんにちは"}' http://127.0.0.1:8080/v1/chat
```

For a real provider set `AI_PROVIDER_URL`, `AI_PROVIDER_API_KEY`, and
`AI_PROVIDER_MODEL`; use HTTPS with certificate verification. Production
deployments should set a strong `DEVICE_TOKEN` and must not enable
`ALLOW_INSECURE_DEV`. Do not commit `.env` files or credentials.

The firmware stores its endpoint and device token in the NVS namespace
`tachi_gateway` (`url`, `device_token`). A development build can provision
these once using local-only CMake cache values:

```powershell
idf.py -D DEVELOPMENT_BUILD=ON `
  -D TACHIKOMA_GATEWAY_URL=https://gateway.example/v1/chat `
  -D TACHIKOMA_DEVICE_TOKEN=<local-token> reconfigure
```

The token is written to NVS on boot and is not printed in logs. Do not commit
the command line or its values.
