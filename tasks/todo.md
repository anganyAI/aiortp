# Lot Comfort Noise RFC 3389 + Opus DTX (pré-P0.2)

Plan complet : ~/.claude/plans/whimsical-coalescing-biscuit.md
Principes : émission opt-in (cn=True, exige paced), réception PT 13 toujours active
(correction protocolaire, même famille que le fix RFC 4733).

## Implémentation
- [x] cn.py : parse/build payload CN, measure_level (RMS → -dBov), NoiseGenerator
      (bruit blanc compensé √3 pour RMS exact)
- [x] pacer.py : paquet CN à l'entrée en silence (après 1er audio), refresh 3 s,
      marker=1 sur la reprise de talkspurt, compteur cn_sent
- [x] playout.py : CN = événement SUR LA TIMELINE (_cn_from) — les frames bufferisées
      avant le point de silence jouent encore, la perte avant conceal encore ;
      bruit à partir de cn_from, sortie sur média ≥ cn_from, reprise de timeline
      suspendue sans re-priming, garde-fou 30 s, compteur cn_frames
- [x] session.py : params cn/cn_payload_type, garde cn⇒paced, EWMA measure_level,
      branche PT 13 toujours active, _handle_non_media partagé DTMF/CN, on_cn
- [x] codecs/opus.py : dtx=True via encoder_ctl bas niveau + property dtx
- [x] tests : test_cn.py (8) + pacer (4) + playout (6) + session (4) + opus dtx
- [x] README : bullet + sous-section CN ; exports NoiseGenerator/parse/build/measure

## Vérification
- [x] pytest : 313 passed +1 skip (opus on), 310+4 (opus off) ; mypy baseline 11 ; ruff ok
- [x] Taille session.py : 309 LOC selon la règle réelle (hors docstrings) — sous 330 ;
      scission toujours prévue avec P0.2
- [x] Sweep temporel propre ; 6 commits atomiques poussés ; CI verte

## Review
- Correction de design en cours de lot : sortie d'état CN à la première frame réelle
  consommée était FAUX (le paquet CN arrive avant que le playout draine les dernières
  frames bufferisées) → CN modélisé comme point sur la timeline (_cn_from), effectif
  à partir de son timestamp. Testé explicitement (test_cn_applies_only_from_its_timestamp).
- Découvertes opuslib : (1) son wrapper _set_dtx envoie la requête GET (bug upstream) ;
  (2) opus_encoder_ctl est variadique → ctypes ne peut pas l'appeler fiablement sur
  Apple Silicon — le flag DTX ne peut être validé que sur Linux (test skipIf darwin/arm64).
  La suppression de silence d'aiortp (paced + CN) n'en dépend pas.
- Pas de release : 0.5.1/0.6.0 groupera CN + les deux fixes cosmétiques précédents.

## Deep review du lot CN (2026-06-11)
- 0 🔴. CN-1 corrigé : le refresh CN (3 s) déplaçait _cn_from devant la tête →
  concealment fantôme à chaque refresh ; origine du silence figée à l'entrée en
  état CN (+ test). CN-2 : _cn → _cn_enabled (homonymie avec le générateur du
  playout). CN-3 différé : 2 branches PT dans _handle_rtp tolérées — extraction
  en registre {pt: handler} au 3e type non-média.
- 314 tests verts, CI verte, sweep propre.

## Release 0.6.0 — FAIT (2026-06-11)
- README audité lib complète avant release : bullet SSRC relatch, section NAT
  Traversal, section Session Statistics (dict complet + on_receiver_report),
  Opus/register_codec dans Codec Registry, renvoi playout dans PLC.
- Tag v0.6.0, publié PyPI (twine), install vérifiée. Contenu : lot CN + fix stats
  RFC 4733 + fix silence-origin + marker talkspurt + Opus dtx.
- Bumps aval (aiosipua/roomkit aiortp>=0.6.0) : pas urgents — 0.6 n'apporte que
  des features opt-in par rapport à 0.5 ; à faire quand roomkit voudra CN/playout.

## Suite (P0.2)
- SRTP/SRTCP (SDES via pylibsrtp, puis DTLS-SRTP) + rtcp-mux (RFC 5761)
