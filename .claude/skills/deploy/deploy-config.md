# Deploy Config

| Environment | Branch  | URL                          | Perintah deploy        |
|-------------|---------|------------------------------|------------------------|
| staging     | develop | https://staging.example.com  | `npm run deploy:stg`   |
| production  | main    | https://example.com          | `npm run deploy:prod`  |

## Catatan

- Secret deploy dibaca dari environment variable, jangan ditulis di file ini.
- Rollback: `npm run deploy:rollback -- <env>`
