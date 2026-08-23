# 🚀 Постоянный хостинг монитора (24/7)

Сайт живёт на here.now — это статический хостинг, он не «умирает».
Умирает **сканер** (процесс Python), которому нужен живой сервер.
Ниже два готовых способа запустить его навсегда. Папка уже готова к обоим.

---

## Вариант 1 — GitHub Actions (бесплатно, без сервера)

Скан и публикация каждые ~5 минут, сайт остаётся тот же самый.

1. Зайди на github.com → **New repository** → имя `kleinanzeigen-monitor` → **Public** (для бесплатных Actions) → Create.
2. Загрузи содержимое этой папки (кнопка **uploading an existing file**; папку `.github` загрузить через веб нельзя — см. шаг 3).
3. Проще всего через консоль один раз:
   ```
   git init && git add . && git commit -m monitor
   git remote add origin https://github.com/ТВОЙ_ЛОГИН/kleinanzeigen-monitor.git
   git push -u origin main
   ```
4. В репозитории: **Settings → Secrets and variables → Actions → New repository secret** — добавь два:
   - `HERENOW_SLUG` = `centered-birch-dbfv`
   - `HERENOW_CLAIM` = (claimToken из data/herenow_claim.txt — см. чат)
5. Вкладка **Actions** → workflow `scan` сам запустится по расписанию (кнопкой **Run workflow** можно проверить сразу).
6. Готово: https://centered-birch-dbfv.here.now/ обновляется сам, база хранится в репозитории.

⚠️ Если Kleinanzeigen начнёт блокировать IP GitHub-раннеров (403 в логах Actions) —
увеличь `request_delay_sec` в config.json до 8–10 или переходи на вариант 2.

---

## Вариант 2 — свой VPS / домашний сервер (Docker, обновление каждые ~2 минуты)

Полный монитор: скан 60 c, публикация 90 c, дашборд на :8080.

```
scp -r kleinanzeigen-monitor/ user@СЕРВЕР:~/
ssh user@СЕРВЕР
cd kleinanzeigen-monitor && docker compose up -d --build
```

Проверка: `docker logs -f kleinanzeigen-monitor`, сайт — https://centered-birch-dbfv.here.now/
Данные (база + токен) лежат в `./data` и переживают всё.

Без Docker (просто Python 3.10+): `nohup python3 server.py 8080 &`

> Если разворачиваешь с чистого клона репозитория (а не с этой папки) — положи в `data/`
> файл `herenow_claim.txt` с токеном сайта (возьми из чата), иначе монитор создаст новый сайт here.now.

---

## Что где

| Файл | Зачем |
|---|---|
| `scan_once.py` | один цикл: скан → просмотры → публикация (для Actions/cron) |
| `.github/workflows/scan.yml` | расписание каждые 5 минут |
| `Dockerfile`, `docker-compose.yml` | вариант для сервера |
| `config.json` | темп сканирования и лимиты замеров просмотров |
