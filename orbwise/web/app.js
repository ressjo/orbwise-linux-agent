/* Orbwise – Web-Client: lädt die Module unter js/ und startet sie.
 *
 * Jedes Modul exportiert seine Funktionen; was beim Laden passieren muss (Werte belegen, Knöpfe verdrahten,
 * erste Abfragen), steht in seinem init(). Die Reihenfolge unten ist die, in der das früher in einer Datei
 * stand – Module dürfen sich gegenseitig importieren, aufgerufen wird erst nach dem Laden aller Module. */

import { init as core } from "./js/core.js";
import { init as audio } from "./js/audio.js";
import { init as ws } from "./js/ws.js";
import { init as chat } from "./js/chat.js";
import { init as activity } from "./js/activity.js";
import { init as coding } from "./js/coding.js";
import { init as mic } from "./js/mic.js";
import { init as password } from "./js/password.js";
import { init as attach } from "./js/attach.js";
import { init as nav } from "./js/nav.js";
import { init as skills } from "./js/skills.js";
import { init as voice } from "./js/voice.js";
import { init as telemetry } from "./js/telemetry.js";
import { init as models } from "./js/models.js";
import { init as planner } from "./js/planner.js";
import { init as history } from "./js/history.js";
import { init as images } from "./js/images.js";
import { init as update } from "./js/update.js";
import { init as modes } from "./js/modes.js";
import { init as project } from "./js/project.js";
import { init as reminders } from "./js/reminders.js";
import { init as startup } from "./js/startup.js";

for (const init of [core, audio, ws, chat, activity, coding, mic, password, attach, nav, skills, voice, telemetry, models, planner, history, images, update, modes, project, reminders, startup]) init();
