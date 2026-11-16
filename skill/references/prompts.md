# Briefs that improve output

The translator's prompt is fixed in the package; these are the *inputs* you control,
and they matter more than any adjective in a system prompt.

## `context` — what the video is about

Bad: `"a video"`.
Good: `"Class 9 biology lesson on photosynthesis, taught to 14-year-olds in a
government school in Bihar"`.

What good context buys you: correct terminology (stomata, chlorophyll), correct
register (a teacher explaining, not a textbook), and correct audience (a 14-year-old,
not a researcher). One sentence is usually enough; a paragraph is not wasted on a
feature-length file.

## `glossary` — pin what must not drift

`glossary="ISRO=इसरो,UPI=यूपीआई,NEP=राष्ट्रीय शिक्षा नीति,2026=दो हज़ार छब्बीस"`

Use it for: acronyms, brand and product names, place names, personal names, numbers
that a voice would otherwise read in English, and any term the client has a preferred
rendering for.

Write the value in the **target script**, phonetically if that is what it takes:
`"Chaudhary=चौधरी"` produces the right pronunciation; `"Chaudhary=Chaudhary"` produces
an English accent in the middle of a Hindi sentence.

## `style` — how it should be said

This reaches both the translator and the voice, which is why it is one field rather
than two.

* `"warm, unhurried documentary narrator"`
* `"respectful teacher addressing a class, आप-form"`
* `"fast, excited sports commentary"`
* `"formal news anchor, neutral tone"`

Name the formality where the language has one: Hindi तू/तुम/आप, Bengali তুই/তুমি/আপনি,
Tamil நீ/நீங்கள், French tu/vous, Japanese plain/keigo. A model will guess, and it
guesses wrong about half the time.

## `emotion`

A per-run hint for voices that accept one (ElevenLabs style, OpenAI instructions).
Use it when the whole file has one emotional colour — a eulogy, a trailer, a children's
story. For mixed material, leave it empty and let `style` carry the register: forcing
one emotion across a whole film is worse than none.

## Prompts for the agent itself

When the user is vague, these are the questions that change the build:

* "Who is speaking, and is it one person or several?" → decides diarisation and voice
  mapping.
* "Will this be watched by students, published, or broadcast?" → decides loudness
  target, voice quality, and whether lip sync is worth it.
* "Is there music or background sound you need to keep?" → decides the separator.
* "Do you have a voice you must use, or should I pick one?" → decides cloning and the
  consent conversation.
* "Roughly how long is the video?" → decides whether to warn about cost and time
  before starting.
