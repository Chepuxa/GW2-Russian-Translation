---
license: cc-by-4.0
tags:
- translation
- ctranslate2
- opus-mt
- unreal-engine
language:
- en
- ru
---

# opus-mt-en-ru (CTranslate2 INT8)

CTranslate2 INT8 quantized conversion of [Helsinki-NLP/opus-mt-en-ru](https://huggingface.co/Helsinki-NLP/opus-mt-en-ru)
for use with the Unreal Engine Offline Translator Plugin.

## Usage

This model is intended to be used with the Unreal Engine Offline Translator plugin.
It translates from **English** to **Russian**.

## Changes made
- Converted to CTranslate2 format using ct2-transformers-converter
- Quantized to INT8 for faster CPU inference
- Optimized for use with oneDNN backend on Intel CPUs

## Performance
- Load time: ~250ms
- Translation speed: ~100ms per sentence (CPU, Intel oneDNN)

## Original model
- Author: Language Technology Research Group, University of Helsinki
- Repository: [Helsinki-NLP/opus-mt-en-ru](https://huggingface.co/Helsinki-NLP/opus-mt-en-ru)
- License: [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/)

## License
This conversion is distributed under the same
[CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/) license as the original.
Attribution: Language Technology Research Group, University of Helsinki.
