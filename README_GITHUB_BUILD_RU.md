# Сборка Audio Splitter для macOS без своего Mac

Этот комплект рассчитан на сборку через GitHub Actions. Сборка выполняется на macOS-раннере GitHub, поэтому локальный Mac не нужен.

## Что внутри

- `audio_splitter_mac.py` — приложение.
- `.github/workflows/build-macos.yml` — автоматическая сборка `.app` для macOS.
- `build_macos_app.sh` — запасной скрипт для сборки на настоящем Mac, если он появится.

## Что получится

После сборки GitHub Actions отдаст два ZIP-файла:

- `Audio Splitter-apple-silicon.zip` — для Mac на Apple Silicon: M1, M2, M3, M4 и новее.
- `Audio Splitter-intel.zip` — для старых Intel Mac.

Внутри ZIP будет `Audio Splitter.app`. FFmpeg и FFprobe уже будут внутри `.app`, отдельно пользователю их класть не нужно.

## Шаги

1. Создайте репозиторий на GitHub. Можно private.
2. Загрузите в репозиторий весь набор файлов из этой папки, включая скрытую папку `.github`.
3. Откройте вкладку `Actions`.
4. Выберите workflow `Build macOS app`.
5. Нажмите `Run workflow`.
6. После завершения откройте выполненный run.
7. Внизу страницы скачайте artifacts:
   - `Audio-Splitter-macOS-apple-silicon`
   - `Audio-Splitter-macOS-intel`
8. Узнайте у пользователя тип процессора Mac:
   - Apple menu -> About This Mac -> Chip: Apple M... => apple-silicon.
   - Apple menu -> About This Mac -> Processor: Intel... => intel.
9. Отправьте пользователю подходящий ZIP.

## Инструкция для пользователя Mac

1. Скачать ZIP.
2. Распаковать ZIP.
3. Перетащить `Audio Splitter.app` в папку `Applications`.
4. Первый запуск: Control-click / правый клик по приложению -> Open -> Open.

Если macOS пишет, что приложение от неизвестного разработчика:

- System Settings -> Privacy & Security -> Open Anyway.
- Затем снова открыть приложение.

## Ограничение

Приложение подписывается ad-hoc подписью, но не notarized через Apple. Для одного доверенного пользователя это обычно достаточно, но macOS может показать предупреждение безопасности при первом запуске.
