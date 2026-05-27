GENRE: LitRPG / system novels. Fiction where the rules of the world are explicitly gamified. The narrative surfaces stat sheets, skill trees, level-up notifications, quest logs, dungeon raids, party mechanics, drop rates, cooldowns. The protagonist's progression is quantified in numbers the reader sees. This overlaps isekai (a transmigrator dropped into a system world) and progression fantasy, but the differentiator is the visibility of the mechanics: a LitRPG chapter typically contains at least one literal system message, one numeric reference, or one explicit skill use.

Narrative mode: close third-person or first-person. The POV is usually the system-aware protagonist. System messages and stat windows interrupt the prose; they are part of the experience, not exposition.

Target prose: contemporary register laced with game vocabulary. Numbers stay numerals. UI artifacts render as UI artifacts.

Voice register:
- Narrator: matter-of-fact about the mechanics. The system is part of physics; the narrator does not editorialize on its weirdness unless the POV is new to it. "His HP dropped to 23." not "The mysterious essence of his life force dwindled to merely 23 units."
- External dialogue: world-appropriate. Players / adventurers / system users speak naturally about levels, classes, drops, cooldowns. The vocabulary is part of how they talk.
- Internal thought: tactical. The POV calculates, checks cooldowns, weighs skill choices, evaluates loot. The reader sees the optimization loop. "If I burn Fireball now, it's down for 30 seconds; better save it for the boss." Render the cost-benefit thinking clearly.

System messages and notifications:
- Render exactly as the source formats them. Preserve **bold**, 【brackets】, ALL-CAPS, line breaks.
- "**【Quest Accepted: Slay the Goblin King】**": Title-Case the field, preserve the 【】, bold the line.
- "[+25 EXP]": preserve the [+...] form when used; render small numerical deltas inline.
- Status blocks (multi-line stat windows) preserve their column structure when possible. If the source uses a table-like layout, render it as a markdown-style aligned block.
- ALL-CAPS for emphasis (CRITICAL HIT, LEVEL UP, BOSS DEFEATED): preserve.

Numerals, strictly preserved:
- HP / MP / SP / EXP / STR / DEX / INT: always uppercase abbreviations.
- "HP 1850 / 2000" stays "HP 1850 / 2000". Do not spell out, do not round, do not "1,850" with commas unless source uses commas.
- Levels: "Level 47" or "Lv. 47", match source.
- Percentages: "30%" not "thirty percent." "+10% crit chance" stays as displayed.
- Time: cooldowns and durations stay numeric. "3 second cooldown" not "three-second cooldown."

Class, skill, item, dungeon names are proper nouns, Title-Cased:
- "Berserker," "Shadow Mage," "Dragon Slayer Sword +5," "Frostfang Caverns."
- Function words mid-name stay lowercase: "Lord of the Black Mire."
- Skill descriptions in italicized infodump blocks preserve the italics.
- Item rarity tiers (Common / Uncommon / Rare / Epic / Legendary / Mythic) Title-Case.

Party / guild / raid vocabulary:
- "Party leader," "main tank," "off-tank," "DPS," "healer," "support," "carry."
- Roles often appear ALL-CAPS in source (DPS, MT, OT, HOT): preserve.
- "Aggro," "threat," "pull," "kite," "burst," "rotation," "uptime," "downtime": preserve as in-genre vocabulary; do not "translate to plain English."

Progression vocabulary:
- "Level up," "class change," "skill up," "evolve," "ascend," "transcend," "break through": render per glossary; preserve the in-system rituals.
- "Grind," "farm," "run," "clear," "wipe," "respawn," "phase": gaming vocabulary used literally; render literal.

Drop rates and probability:
- "1% drop rate," "guaranteed drop," "RNG-dependent," "Mythic-tier RNG": preserve as displayed; do not soften.

Watch-list:
- Do NOT prettify the UI text. "**【Level Up! You are now Level 23.】**" stays that way; do not rewrite as "And so, the protagonist felt himself become stronger."
- Do NOT spell out the numbers in the system text. EVER.
- Do NOT explain the mechanics in-narration. If the source assumes the reader knows what "aggro" means, the target does too.
- Do NOT collapse the gaming vocabulary into generic prose. "He used Fireball" is not "He summoned a ball of flame and hurled it." Skill names are proper nouns; their effects are mechanical, not magical.
- HUD elements stay as HUD elements: a "Status Window" is a UI element, not a literal window in the world (unless the source treats it that way, in which case it's a literal window AND a UI element; match the source's framing).
