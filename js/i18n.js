/**
 * The page's words, in Danish and in English.
 *
 * Which of the two is decided once per visit: the one the visitor chose before,
 * or else the browser's own language — Danish for a Danish browser, English for
 * anybody else. Changing it is remembered and reloads the page, so nothing that
 * is already on screen has to be translated in place.
 *
 * Nothing is looked up when a module is imported. Every string is read when it
 * is shown, which is what lets a test pin the language before it renders.
 *
 * A text is either a string or a function of the values it names, so plurals
 * and word order stay each language's own business rather than a template's.
 */

export const LANGUAGES = ['da', 'en'];

/** Where the visitor's choice of language is remembered. */
const LANGUAGE_KEY = 'koalitionsberegner.language';

const DA_MONTHS = ['januar', 'februar', 'marts', 'april', 'maj', 'juni',
  'juli', 'august', 'september', 'oktober', 'november', 'december'];
const EN_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'];

export const STRINGS = {
  da: {
    // Page chrome
    'language.label': 'Sprog',
    'picker.label': 'Valg',
    'picker.forecast': ({ where, label }) => `${where} · prognose ${label}`,
    'picker.public': 'offentlig',
    'curate.title': 'Vis valget for besøgende, der ikke er logget ind',
    'curate.label': 'Offentlig',
    'names.label': 'Partinavne',
    'names.local': 'Originalsprog',
    'names.english': 'Engelsk',
    'reset': 'Ryd alle',

    // Calculator
    'date': ({ day, month, year }) => `${day}. ${DA_MONTHS[month - 1]} ${year}`,
    'calc.subtitle': ({ majority, total }) =>
      `Vælg partier og se om de tilsammen opnår flertal (${majority}+ ud af ${total} mandater)`,
    'calc.totalOf': ({ total }) => `af ${total} mandater`,
    'calc.majorityNeeds': ({ majority }) => `Flertal kræver ${majority} mandater`,
    'calc.forecastFrom': ({ publisher, date }) => `Prognose fra ${publisher}, ${date}`,
    'calc.finalResult': ({ date }) => `Endelig resultat, ${date}`,
    'calc.largeMajority': 'Stort flertal ✓',
    'calc.majority': ({ over }) => `Flertal ✓ (+${over})`,
    'calc.short': ({ missing }) => `Mangler ${missing}`,
    'seats': ({ count }) => `${count} mandater`,
    'seats.computed': 'mandater beregnet ud fra stemmeandele',

    // Account panel
    'account.summary': 'Konto og abonnement',
    'account.freeNote': 'Det er gratis at oprette en konto. Abonnement kræves kun for at importere nye valg.',
    'account.manage': 'Administrér abonnement',
    'account.signOut': 'Log ud',
    'account.loadFailed': ({ message }) => `Kunne ikke hente kontoen: ${message}`,
    'email.label': 'E-mail',
    'email.missing': 'Angiv din e-mailadresse.',
    'email.invalid': 'Adressen ser ikke ud til at være en e-mailadresse.',
    'password.label': 'Adgangskode',
    'password.missing': 'Angiv en adgangskode.',
    'password.short': ({ min }) => `Adgangskoden skal være mindst ${min} tegn.`,
    'password.forgot': 'Glemt adgangskode?',
    'auth.signin.submit': 'Log ind',
    'auth.signin.switchText': 'Ny her?',
    'auth.signin.switchLabel': 'Opret en konto',
    'auth.signup.submit': 'Opret konto',
    'auth.signup.switchText': 'Har du allerede en konto?',
    'auth.signup.switchLabel': 'Log ind',
    'tier.free': 'Gratis',
    'tier.basic': 'Basis',
    'tier.premium': 'Premium',
    'quota.adminUnlimited': 'Ubegrænsede importer som administrator.',
    'quota.unlimited': 'Ubegrænsede importer (adgangskontrol er slået fra).',
    'quota.none': 'Din plan giver ikke adgang til at importere nye valg.',
    'quota.remaining': ({ remaining, limit }) =>
      `${remaining} af ${limit} ${remaining === 1 ? 'import' : 'importer'} tilbage denne måned.`,
    'upgrade.button': ({ tier, imports }) => `${tier} — ${imports} importer/md.`,
    'payments.paused': 'Betaling virker ikke lige nu — prøv igen i morgen. '
      + 'I mellemtiden kan du skrive under "Importér et valg", hvilket valg du mangler: '
      + 'det bliver noteret som et ønske og importeret manuelt.',
    'signup.created': 'Kontoen er oprettet.',
    'signup.linkSent': ({ email }) =>
      `Kontoen er oprettet. Vi har sendt et link til ${email} — åbn det for at bekræfte adressen.`,
    'signup.linkFailed': 'Kontoen er oprettet. Vi kunne ikke sende bekræftelsesmailen lige nu — '
      + 'tryk på »Send linket igen« om et øjeblik.',
    'reset.sent': ({ email }) =>
      `Hvis der findes en konto for ${email}, har vi sendt et link til at vælge en ny adgangskode.`,
    'verify.note': 'Bekræft din e-mailadresse med linket, vi har sendt dig, før du bruger kontoen. '
      + 'Kan du ikke finde mailen, så kig i spam.',
    'verify.done': 'Jeg har bekræftet',
    'verify.resend': 'Send linket igen',
    'verify.confirmed': 'Tak, din e-mailadresse er bekræftet.',
    'verify.notYet': 'Adressen er ikke bekræftet endnu. Åbn linket i mailen, og prøv igen.',
    'verify.resent': ({ email }) => `Vi har sendt et nyt link til ${email}.`,
    'verify.yourAddress': 'din e-mailadresse',
    'verify.recentlySent': 'Vi har for nylig sendt dig et link. Tjek din indbakke og spam-mappen, '
      + 'eller prøv igen om et par minutter.',
    'checkout.opening': 'Åbner betaling…',
    'checkout.failed': ({ message }) => `Kunne ikke starte betalingen: ${message}`,
    'portal.opening': 'Åbner abonnementet…',
    'portal.failed': ({ message }) => `Kunne ikke åbne abonnementet: ${message}`,
    'login.notConfigured': 'Login er ikke konfigureret på denne server.',

    // Sign-in service
    'auth.emailExists': 'Der findes allerede en konto med den e-mailadresse.',
    'auth.emailNotFound': 'Vi kunne ikke finde en konto med den e-mailadresse.',
    'auth.wrongPassword': 'Forkert adgangskode.',
    'auth.wrongCredentials': 'Forkert e-mailadresse eller adgangskode.',
    'auth.disabled': 'Kontoen er deaktiveret.',
    'auth.tooManyAttempts': 'For mange forsøg. Prøv igen om lidt.',
    'auth.sessionExpired': 'Din session er udløbet. Log ind igen.',
    'auth.failed': 'Log ind mislykkedes. Prøv igen.',
    'auth.unreachable': ({ message }) => `Kunne ikke nå login-tjenesten: ${message}`,
    'auth.signInToConfirm': 'Log ind for at bekræfte din e-mailadresse.',

    // Import form
    'import.summary': 'Importér et valg',
    'import.year': 'Valgår',
    'import.nation': 'Land',
    'import.nationPlaceholder': 'Danmark',
    'import.subnation': 'Region (kun ved regionale valg)',
    'import.subnationPlaceholder': 'fx Sachsen-Anhalt',
    'import.hint': 'Stavefejl er i orden. Vi finder valget og viser, hvilket valg vi fandt — og hvor '
      + 'tallene kommer fra — før der gemmes noget. Er valget ikke afholdt endnu, kan du vælge '
      + 'mellem de seneste meningsmålinger.',
    'import.submit': 'Hent valgresultat',
    'form.yearMissing': 'Angiv valgåret.',
    'form.yearInvalid': ({ min, max }) => `Året skal være et årstal mellem ${min} og ${max}.`,
    'form.nationMissing': 'Angiv landet.',
    'import.refusal.signIn': 'Log ind for at importere eller ønske et valg.',
    'import.refusal.subscription': 'Import kræver et abonnement. Vælg en plan ovenfor.',
    'import.refusal.usedUp': 'Denne måneds importer er brugt op. Kvoten fornys ved månedsskiftet.',
    'import.requestNote': 'Uden abonnement henter vi ikke valget automatisk. '
      + 'Vi skriver det op som et ønske, og det bliver importeret manuelt.',
    'import.remaining': ({ remaining }) =>
      `${remaining} ${remaining === 1 ? 'import' : 'importer'} tilbage denne måned.`,
    'import.alreadyImported': 'Dette valg er allerede importeret.',
    'import.alreadyImportedPick': 'Dette valg er allerede importeret. Vælg det i listen ovenfor.',
    'import.failed': ({ error }) => `Kunne ikke finde valgresultatet: ${error}`,
    'import.pending': 'Behandling er stadig i gang. Prøv igen om lidt.',
    'busy.checking': 'Tjekker…',
    'busy.fetching': 'Henter…',
    'busy.requesting': 'Sender ønske…',
    'error.unexpected': ({ message }) => `Uventet fejl: ${message}`,
    'archive.unreachable': 'Valgarkivet kan ikke nås — viser kun det indbyggede valg.',

    // Requests
    'request.refusal.signIn': 'Log ind for at ønske et valg.',
    'request.refusal.confirmEmail': 'Bekræft din e-mailadresse, før du kan ønske et valg.',
    'request.refusal.unavailable': 'Ønskelisten er ikke tilgængelig lige nu. Prøv igen senere.',
    'request.duplicate': ({ number }) => `Det valg er der allerede et ønske om (#${number}).`,
    'request.filed': ({ number }) => `Ønsket er noteret som #${number}.`,
    'request.byHand': 'Valget bliver importeret manuelt.',
    'request.link': 'Se ønsket',
    'request.failed': ({ message }) => `Kunne ikke sende ønsket: ${message}`,

    // Preview and polls
    'preview.forecastFor': ({ label, identity }) => `Prognose fra ${label} for valget ${identity}`,
    'preview.majorityAt': ({ majority }) => `flertal ved ${majority}`,
    'preview.sourceComputed': ({ url }) =>
      `Stemmeandelene er læst fra ${url}; mandaterne er beregnet ud fra dem og er et skøn.`,
    'preview.source': ({ url }) => `Tallene er læst fra ${url}`,
    'preview.check': 'Kontrollér at det er det rigtige valg, og at tallene passer, før du gemmer.',
    'preview.confirm': 'Gem valget',
    'preview.backToList': 'Tilbage til listen',
    'discard': 'Forkast',
    'discarded': 'Forkastet. Intet blev gemt.',
    'choices.hint': 'Vælg en meningsmåling. Du ser tallene, før noget gemmes.',
    'choices.title': ({ where, date }) => `${where} · valget afholdes senest ${date}`,
    'choices.message': 'Valget er ikke afholdt endnu. Vælg en meningsmåling at regne på.',
    'choices.check': 'Kontrollér tallene, før du gemmer prognosen.',
    'saved.election': 'Valget er gemt.',
    'saved.electionAlready': 'Valget var allerede gemt.',
    'saved.forecast': 'Prognosen er gemt.',
    'saved.forecastAlready': 'Prognosen var allerede gemt.',
    'saved.failed': ({ message }) => `Kunne ikke gemme valget: ${message}`,
    'curate.nowPublic': 'Valget er nu synligt for alle, også uden login.',
    'curate.nowPrivate': 'Valget vises nu kun for brugere, der er logget ind.',
    'curate.failed': ({ message }) => `Kunne ikke ændre synligheden: ${message}`,
    'select.failed': ({ message }) => `Kunne ikke hente valget: ${message}`,
  },

  en: {
    // Page chrome
    'language.label': 'Language',
    'picker.label': 'Election',
    'picker.forecast': ({ where, label }) => `${where} · forecast ${label}`,
    'picker.public': 'public',
    'curate.title': 'Show this election to visitors who are not signed in',
    'curate.label': 'Public',
    'names.label': 'Party names',
    'names.local': 'Original language',
    'names.english': 'English',
    'reset': 'Clear all',

    // Calculator
    'date': ({ day, month, year }) => `${day} ${EN_MONTHS[month - 1]} ${year}`,
    'calc.subtitle': ({ majority, total }) =>
      `Pick parties and see whether together they reach a majority (${majority}+ of ${total} seats)`,
    'calc.totalOf': ({ total }) => `of ${total} seats`,
    'calc.majorityNeeds': ({ majority }) => `A majority needs ${majority} seats`,
    'calc.forecastFrom': ({ publisher, date }) => `Forecast by ${publisher}, ${date}`,
    'calc.finalResult': ({ date }) => `Final result, ${date}`,
    'calc.largeMajority': 'Large majority ✓',
    'calc.majority': ({ over }) => `Majority ✓ (+${over})`,
    'calc.short': ({ missing }) => `${missing} short`,
    'seats': ({ count }) => `${count} ${count === 1 ? 'seat' : 'seats'}`,
    'seats.computed': 'seats calculated from vote shares',

    // Account panel
    'account.summary': 'Account and subscription',
    'account.freeNote': 'Creating an account is free. A subscription is only needed to import new elections.',
    'account.manage': 'Manage subscription',
    'account.signOut': 'Sign out',
    'account.loadFailed': ({ message }) => `Couldn't load your account: ${message}`,
    'email.label': 'Email',
    'email.missing': 'Enter your email address.',
    'email.invalid': "That doesn't look like an email address.",
    'password.label': 'Password',
    'password.missing': 'Enter a password.',
    'password.short': ({ min }) => `The password must be at least ${min} characters.`,
    'password.forgot': 'Forgot your password?',
    'auth.signin.submit': 'Sign in',
    'auth.signin.switchText': 'New here?',
    'auth.signin.switchLabel': 'Create an account',
    'auth.signup.submit': 'Create account',
    'auth.signup.switchText': 'Already have an account?',
    'auth.signup.switchLabel': 'Sign in',
    'tier.free': 'Free',
    'tier.basic': 'Basic',
    'tier.premium': 'Premium',
    'quota.adminUnlimited': 'Unlimited imports as an administrator.',
    'quota.unlimited': 'Unlimited imports (access control is off).',
    'quota.none': "Your plan doesn't include importing new elections.",
    'quota.remaining': ({ remaining, limit }) =>
      `${remaining} of ${limit} ${limit === 1 ? 'import' : 'imports'} left this month.`,
    'upgrade.button': ({ tier, imports }) => `${tier} — ${imports} imports/month`,
    'payments.paused': "Payments aren't working right now — try again tomorrow. "
      + 'Meanwhile, enter the election you are missing under "Import an election": '
      + 'it is noted as a request and imported by hand.',
    'signup.created': 'Your account has been created.',
    'signup.linkSent': ({ email }) =>
      `Your account has been created. We've sent a link to ${email} — open it to confirm your address.`,
    'signup.linkFailed': "Your account has been created. We couldn't send the confirmation email just now — "
      + 'press "Send the link again" in a moment.',
    'reset.sent': ({ email }) =>
      `If there is an account for ${email}, we've sent it a link to choose a new password.`,
    'verify.note': 'Confirm your email address with the link we sent you before using the account. '
      + "If you can't find the email, check your spam folder.",
    'verify.done': "I've confirmed it",
    'verify.resend': 'Send the link again',
    'verify.confirmed': 'Thanks, your email address is confirmed.',
    'verify.notYet': "The address isn't confirmed yet. Open the link in the email and try again.",
    'verify.resent': ({ email }) => `We've sent a new link to ${email}.`,
    'verify.yourAddress': 'your email address',
    'verify.recentlySent': 'We sent you a link a moment ago. Check your inbox and spam folder, '
      + 'or try again in a few minutes.',
    'checkout.opening': 'Opening payment…',
    'checkout.failed': ({ message }) => `Couldn't start the payment: ${message}`,
    'portal.opening': 'Opening your subscription…',
    'portal.failed': ({ message }) => `Couldn't open your subscription: ${message}`,
    'login.notConfigured': "Sign-in isn't configured on this server.",

    // Sign-in service
    'auth.emailExists': 'There is already an account with that email address.',
    'auth.emailNotFound': "We couldn't find an account with that email address.",
    'auth.wrongPassword': 'Wrong password.',
    'auth.wrongCredentials': 'Wrong email address or password.',
    'auth.disabled': 'This account has been disabled.',
    'auth.tooManyAttempts': 'Too many attempts. Try again in a moment.',
    'auth.sessionExpired': 'Your session has expired. Please sign in again.',
    'auth.failed': 'Sign-in failed. Please try again.',
    'auth.unreachable': ({ message }) => `Couldn't reach the sign-in service: ${message}`,
    'auth.signInToConfirm': 'Sign in to confirm your email address.',

    // Import form
    'import.summary': 'Import an election',
    'import.year': 'Election year',
    'import.nation': 'Country',
    'import.nationPlaceholder': 'Denmark',
    'import.subnation': 'Region (regional elections only)',
    'import.subnationPlaceholder': 'e.g. Saxony-Anhalt',
    'import.hint': "Typos are fine. We find the election and show which one we found — and where the "
      + "numbers come from — before anything is saved. If the election hasn't been held yet, you can "
      + 'choose from the latest opinion polls.',
    'import.submit': 'Get the results',
    'form.yearMissing': 'Enter the election year.',
    'form.yearInvalid': ({ min, max }) => `The year must be between ${min} and ${max}.`,
    'form.nationMissing': 'Enter the country.',
    'import.refusal.signIn': 'Sign in to import or request an election.',
    'import.refusal.subscription': 'Importing requires a subscription. Pick a plan above.',
    'import.refusal.usedUp': "This month's imports are used up. They renew at the start of next month.",
    'import.requestNote': "Without a subscription we don't fetch the election automatically. "
      + 'We note it down as a request, and it is imported by hand.',
    'import.remaining': ({ remaining }) =>
      `${remaining} ${remaining === 1 ? 'import' : 'imports'} left this month.`,
    'import.alreadyImported': 'This election has already been imported.',
    'import.alreadyImportedPick': 'This election has already been imported. Pick it in the list above.',
    'import.failed': ({ error }) => `Couldn't find the results: ${error}`,
    'import.pending': 'Still processing. Try again in a moment.',
    'busy.checking': 'Checking…',
    'busy.fetching': 'Fetching…',
    'busy.requesting': 'Sending request…',
    'error.unexpected': ({ message }) => `Unexpected error: ${message}`,
    'archive.unreachable': "The election archive can't be reached — showing only the built-in election.",

    // Requests
    'request.refusal.signIn': 'Sign in to request an election.',
    'request.refusal.confirmEmail': 'Confirm your email address before requesting an election.',
    'request.refusal.unavailable': "Requests aren't available right now. Please try again later.",
    'request.duplicate': ({ number }) => `That election has already been requested (#${number}).`,
    'request.filed': ({ number }) => `Your request is noted as #${number}.`,
    'request.byHand': 'The election will be imported by hand.',
    'request.link': 'See the request',
    'request.failed': ({ message }) => `Couldn't send the request: ${message}`,

    // Preview and polls
    'preview.forecastFor': ({ label, identity }) => `Forecast by ${label} for the election ${identity}`,
    'preview.majorityAt': ({ majority }) => `majority at ${majority}`,
    'preview.sourceComputed': ({ url }) =>
      `The vote shares were read from ${url}; the seats are calculated from them and are an estimate.`,
    'preview.source': ({ url }) => `The numbers were read from ${url}`,
    'preview.check': 'Check that this is the right election and that the numbers are right before you save.',
    'preview.confirm': 'Save the election',
    'preview.backToList': 'Back to the list',
    'discard': 'Discard',
    'discarded': 'Discarded. Nothing was saved.',
    'choices.hint': "Pick an opinion poll. You'll see the numbers before anything is saved.",
    'choices.title': ({ where, date }) => `${where} · election due by ${date}`,
    'choices.message': "This election hasn't been held yet. Pick an opinion poll to work with.",
    'choices.check': 'Check the numbers before you save the forecast.',
    'saved.election': 'The election is saved.',
    'saved.electionAlready': 'The election was already saved.',
    'saved.forecast': 'The forecast is saved.',
    'saved.forecastAlready': 'The forecast was already saved.',
    'saved.failed': ({ message }) => `Couldn't save the election: ${message}`,
    'curate.nowPublic': 'The election is now visible to everyone, signed in or not.',
    'curate.nowPrivate': 'The election is now only shown to signed-in users.',
    'curate.failed': ({ message }) => `Couldn't change the visibility: ${message}`,
    'select.failed': ({ message }) => `Couldn't load the election: ${message}`,
  },
};

/** The language to use, given what was saved and what the browser says. */
export function detectLanguage({ saved, browser } = {}) {
  if (LANGUAGES.includes(saved)) return saved;
  return String(browser ?? '').toLowerCase().startsWith('da') ? 'da' : 'en';
}

let current = null;

function readSaved() {
  try {
    return globalThis.localStorage?.getItem(LANGUAGE_KEY) ?? null;
  } catch {
    return null;
  }
}

/** `da` or `en`; read once, kept for the visit even where storage fails. */
export function language() {
  current ??= detectLanguage({ saved: readSaved(), browser: globalThis.navigator?.language });
  return current;
}

/**
 * Use `lang` from now on. `remember: false` is for tests, which have no
 * visitor whose choice is worth keeping.
 */
export function setLanguage(lang, { remember = true } = {}) {
  current = LANGUAGES.includes(lang) ? lang : 'en';
  if (remember) {
    try {
      globalThis.localStorage?.setItem(LANGUAGE_KEY, current);
    } catch {
      // Private windows and blocked storage: the choice lasts until the page is left.
    }
  }
  return current;
}

/**
 * The text for `key` in the current language. A key with no text is shown as
 * itself — a visible slip, not a broken page; the i18n tests keep it from
 * shipping.
 */
export function t(key, vars = {}) {
  const text = STRINGS[language()][key];
  if (text === undefined) return key;
  return typeof text === 'function' ? text(vars) : text;
}
