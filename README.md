# BardForge Community

Общие материалы организации [BardForge](https://github.com/BardForge): профиль, правила участия, формы задач и шаблон PR.

- [Профиль проекта](profile/README.md)
- [Как участвовать](CONTRIBUTING.md)
- [Правила общения](CODE_OF_CONDUCT.md)
- [Безопасность](SECURITY.md)
- [Ограничения](DISCLAIMER.md)
- [Единая доска разработки](https://github.com/orgs/BardForge/projects/1)

Основной язык документации — русский. Код, ветки, коммиты и технические идентификаторы — английские. Новые материалы проходят PR в `dev`; `main` зарезервирована для утверждённого состояния. Основной репозиторий перенесён в [BardForge/bardforge](https://github.com/BardForge/bardforge). Код лицензирован по [MIT](LICENSE); см. [границы лицензии](LICENSING.md).

## Проверка

```sh
npm ci
npm run check
```

Проверяется формат Markdown/YAML/JSON и состав Git на служебные файлы и известные форматы секретов. Это не полная проверка безопасности.

## AI-Assisted Development

BardForge is developed with assistance from artificial intelligence tools. AI may contribute to code, documentation, testing, and technical design. All contributions remain subject to human review, validation, and applicable licensing requirements.
