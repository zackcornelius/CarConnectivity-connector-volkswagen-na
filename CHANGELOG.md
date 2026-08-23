# Changelog

All notable changes to this project will be documented in this file. See [Conventional Commits](https://conventionalcommits.org) for commit guidelines.

# [0.2.0](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.25...v0.2.0) (2026-08-23)


### Bug Fixes

* revert charging current to min=10/max=32/precision=22 ([490ee9b](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/490ee9b78e8fc03a45ff94c866ce2ceb9ca3773c)), closes [#69](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/issues/69)
* suppress bandit B105 false positive on PI sentinel value ([f861d18](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/f861d18458865e4d3aafb5f0b96a12bbebf87aa2))


### Features

* accept Play Integrity token from config/relay ([26fa293](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/26fa2933f3b122b7de3ac516c094d72299aea171))
* add remote engine start/stop for ICE/hybrid vehicles ([58cd020](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/58cd020eb2adf1eed720d557b0f4ee04f2e7e379))

## [0.1.25](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.24...v0.1.25) (2026-08-03)


### Bug Fixes

* **lint:** narrow exception and log instead of bare pass ([a5c2217](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/a5c2217091a71ac4d517f7987a53d6c84c882555))
* **auth:** use backend-local 'sub' as user id on CA endpoints ([f708b54](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/f708b54080ca1712e61c1dc901b1e6429ab1c5e5)), closes [#76](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/issues/76)

## [0.1.24](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.23...v0.1.24) (2026-07-31)


### Bug Fixes

* **auth:** send play_integrity_token on token grants (VW change 2026-07-30) ([b6c90e9](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/b6c90e9c23aea2c1428950d85f0f745d2e4f4b96))

## [0.1.23](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.22...v0.1.23) (2026-07-27)


### Bug Fixes

* normalize climate temperature to Celsius for consistent HA display ([64360ac](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/64360acd58c97b5e60f5c968a5fb92f0b4bab1c3))
* set climate settings as changeable before firing ENABLED event ([ba03aab](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/ba03aabe6058a24a78a11e37410700bd62d199c0))

## [0.1.22](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.21...v0.1.22) (2026-06-29)


### Bug Fixes

* **auth:** impersonate current myVW Android app to restore NA backend access ([57c7c04](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/57c7c047bfb6d83457b62252da6526f8057d8298))

## [0.1.21](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.20...v0.1.21) (2026-05-26)


### Bug Fixes

* **ci:** Update dependencies ([d0737ad](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/d0737ad77a7c9163e70c63edd9bac618652c1b52))

## [0.1.20](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.19...v0.1.20) (2026-05-23)


### Bug Fixes

* **ci:** Trigger a new build ([4215c3d](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/4215c3dbd6bff0098ef4ddcbe400850cd9a1b07b))

## [0.1.18](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.17...v0.1.18) (2026-05-03)


### Bug Fixes

* **auth:** Request full OAuth scopes to resolve 403/401 errors ([afc9058](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/afc90586fd246b6132578daebb18991b48609568)), closes [#66](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/issues/66)
* resolve requests dependency conflict with carconnectivity >= 0.11.9 ([3359ba1](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/3359ba11b3262026e2e76ec5718453304d0412ad))
* **SPIN:** Use access_token instead of id_token for Bearer auth ([45b838c](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/45b838cf2a7f70f1c1381dcfe6071aca9dcfe2d0))

## [0.1.17](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.16...v0.1.17) (2026-04-01)


### Bug Fixes

* **SPIN:** Use country-specific TSP value ([321bd6f](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/321bd6f48579fca156f495e6cb574fcf084f8b3d)), closes [#57](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/issues/57)

## [0.1.16](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.15...v0.1.16) (2026-03-02)


### Bug Fixes

* **auth:** Proactive token refresh and 403/401 retry logic ([412b4b0](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/412b4b00bc5e4bc06481832e4f227560295007e9))

## [0.1.15](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.14...v0.1.15) (2026-02-27)


### Bug Fixes

* **SPIN:** Try and set SPIN if 404/403 ([75f5d3f](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/75f5d3f929470bd8b9270000bc0527c628765b11))
* Update README ([86623ed](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/86623ede0b559c38c53b3ecf4f507287abff6d59))

## [0.1.15-b.2](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.15-b.1...v0.1.15-b.2) (2026-02-23)


### Bug Fixes

* Update README ([86623ed](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/86623ede0b559c38c53b3ecf4f507287abff6d59))

## [0.1.15-b.1](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.14...v0.1.15-b.1) (2026-02-16)


### Bug Fixes

* **SPIN:** Try and set SPIN if 404/403 ([75f5d3f](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/75f5d3f929470bd8b9270000bc0527c628765b11))

## [0.1.14](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.13...v0.1.14) (2026-02-16)


### Bug Fixes

* **ev:** Fix climate and charge values ([3849e77](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/3849e7738678406dc7fd2c24e609d6888d5ef759))
* **ev:** Fix climate and charge values ([9e3c998](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/9e3c998acc1db020a2f6d6afe9f1949931ee18df))
* **format:** Fix overrides for post, put, delete functions ([962de38](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/962de3874293fe38e98d922c2776e7efbf71c9f0))
* **ci:** Fixup version tags to match PEP 440 format ([91371f0](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/91371f07ef24c65dfeb6b216cd27b490125ac712))
* **ci:** Force new build on main ([72ffb39](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/72ffb39ba6c1c7cc48d54f284def1e1b7729c8bd))
* **ci:** Set python build to exact tag version ([8a3c567](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/8a3c567d109b6217abfd3e1892f52c848de5b9a2))
* **ci:** Trigger a patch build for beta ([0860491](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/0860491427d5b3e546f01529b86cf0e5b21abd23))

## [0.1.14-b.1](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.13...v0.1.14-b.1) (2026-02-16)


### Bug Fixes

* **ev:** Fix climate and charge values ([3849e77](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/3849e7738678406dc7fd2c24e609d6888d5ef759))
* **ev:** Fix climate and charge values ([9e3c998](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/9e3c998acc1db020a2f6d6afe9f1949931ee18df))
* **ci:** Fixup version tags to match PEP 440 format ([91371f0](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/91371f07ef24c65dfeb6b216cd27b490125ac712))
* **ci:** Set python build to exact tag version ([8a3c567](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/8a3c567d109b6217abfd3e1892f52c848de5b9a2))
* **ci:** Trigger a patch build for beta ([0860491](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/0860491427d5b3e546f01529b86cf0e5b21abd23))

## [0.1.14-beta.3](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.14-beta.2...v0.1.14-beta.3) (2026-02-16)


### Bug Fixes

* **ci:** Set python build to exact tag version ([8a3c567](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/8a3c567d109b6217abfd3e1892f52c848de5b9a2))

## [0.1.14-beta.2](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.14-beta.1...v0.1.14-beta.2) (2026-02-16)


### Bug Fixes

* **ev:** Fix climate and charge values ([3849e77](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/3849e7738678406dc7fd2c24e609d6888d5ef759))
* **ev:** Fix climate and charge values ([9e3c998](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/9e3c998acc1db020a2f6d6afe9f1949931ee18df))

## [0.1.14-beta.1](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/compare/v0.1.13...v0.1.14-beta.1) (2026-02-15)


### Bug Fixes

* **ci:** Trigger a patch build for beta ([0860491](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/commit/0860491427d5b3e546f01529b86cf0e5b21abd23))

# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]
- No unreleased changes so far

## [0.8.2] - 2025-07-22
### Fixed
- Fixes attribute for estimated time reached for charging

## [0.8.1] - 2025-06-27
### Added
- Fix support for adblue range

## [0.8] - 2025-06-26
### Added
- Support for adblue range

## [0.7.3] - 2025-06-20
### Fixed
- Fixes bug that registers hooks several times, causing multiple calls to the servers

### Changed
- Updated dependencies

## [0.7.2] - 2025-04-19
### Fixed
- Fix for problems introduced with PyJWT

## [0.7.1] - 2025-04-19
### Changed
- Use PyJWT instead of jwt

## [0.7] - 2025-04-17
### Changed
- Updated dependencies
- stripping of leading and trailing spaces in commands

### Added
- Precision for all attributes is now used when displaying values

## [0.6] - 2025-04-02
### Fixed
- Made changes to charging settings for older electric vehicles
- Allowes to have multiple instances of this connector running

### Changed
- Updated dependencies

## [0.5] - 2025-03-20
### Added
- Support for window heating attributes
- Support for window heating command
- SUpport for changing charging settings
