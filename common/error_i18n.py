# ruff: noqa: E501
"""The API's fixed error messages in French and Italian (X-01, L-6).

`build_envelope` reads the request's language (`LocaleMiddleware`, from the
`Accept-Language` the BFF forwards) and swaps a message for its translation
here, by its exact English. Every `OCCSError`'s `default_detail` has an entry
(`common/tests/test_error_i18n.py` walks them); a one-off message raised with
its own words stays English until it gets an entry. Django's and DRF's own
messages are translated by their shipped catalogues.

The words await a native speaker's review (X-01).
"""

from __future__ import annotations

from django.utils import translation

#: English → (French, Italian).
ERRORS: dict[str, tuple[str, str]] = {
    "The request could not be completed.": (
        "La demande n'a pas pu aboutir.",
        "Non è stato possibile completare la richiesta.",
    ),
    "Your plan does not include this.": (
        "Votre offre ne comprend pas cela.",
        "Il tuo piano non lo include.",
    ),
    "This feature is not available on your plan.": (
        "Cette fonctionnalité n'est pas disponible avec votre offre.",
        "Questa funzione non è disponibile nel tuo piano.",
    ),
    "Your plan does not allow that many people or products in one ad.": (
        "Votre offre n'autorise pas autant de personnes ou de produits dans une publicité.",
        "Il tuo piano non consente così tante persone o prodotti in un annuncio.",
    ),
    "This ad has used all its re-rolls.": (
        "Cette publicité a utilisé tous ses nouveaux essais.",
        "Questo annuncio ha esaurito i suoi tentativi.",
    ),
    "This brand has all the avatars its plan allows.": (
        "Cette marque a déjà tous les avatars que son offre permet.",
        "Questo brand ha già tutti gli avatar che il suo piano consente.",
    ),
    "This avatar is locked: the plan allows fewer avatars than the brand has.": (
        "Cet avatar est verrouillé : l'offre permet moins d'avatars que la marque n'en a.",
        "Questo avatar è bloccato: il piano consente meno avatar di quanti ne abbia il brand.",
    ),
    "No portrait batches left this month.": (
        "Plus aucune série de portraits ce mois-ci.",
        "Nessuna serie di ritratti rimasta questo mese.",
    ),
    "You do not have enough credits for this generation.": (
        "Vous n'avez pas assez de crédits pour cette génération.",
        "Non hai abbastanza crediti per questa generazione.",
    ),
    "You do not have enough video allowance for this generation.": (
        "Votre quota vidéo ne suffit pas pour cette génération.",
        "La tua quota video non basta per questa generazione.",
    ),
    "You have reached your plan's limit for this resource.": (
        "Vous avez atteint la limite de votre offre pour cet élément.",
        "Hai raggiunto il limite del tuo piano per questa risorsa.",
    ),
    "That range reaches further back than this plan keeps analytics.": (
        "Cette période remonte plus loin que l'historique d'analyse de cette offre.",
        "Questo intervallo va più indietro di quanto il piano conservi le analisi.",
    ),
    "The resource is not in a state that allows this transition.": (
        "L'élément n'est pas dans un état qui permet ce changement.",
        "La risorsa non è in uno stato che consente questo passaggio.",
    ),
    "This connected account cannot publish right now.": (
        "Ce compte connecté ne peut pas publier pour le moment.",
        "Questo account collegato non può pubblicare in questo momento.",
    ),
    "This post is locked; it was approved and cannot be edited.": (
        "Cette publication est verrouillée : elle a été validée et ne peut plus être modifiée.",
        "Questo post è bloccato: è stato approvato e non può essere modificato.",
    ),
    "This post is no longer at that approval stage.": (
        "Cette publication n'en est plus à cette étape de validation.",
        "Questo post non è più a quella fase di approvazione.",
    ),
    "The approver no longer has permission to approve posts in this workspace.": (
        "Cette personne n'a plus le droit de valider des publications dans cet espace de travail.",
        "Questa persona non ha più il permesso di approvare post in questo spazio di lavoro.",
    ),
    "An export is already being prepared.": (
        "Un export est déjà en préparation.",
        "Un'esportazione è già in preparazione.",
    ),
    "You are the only owner of an organization other people work in.": (
        "Vous êtes le seul propriétaire d'une organisation où d'autres personnes travaillent.",
        "Sei l'unico proprietario di un'organizzazione in cui lavorano altre persone.",
    ),
    "Cancel your paid plan in Credits & plan before deleting your account.": (
        "Résiliez votre offre payante dans Crédits et offre avant de supprimer votre compte.",
        "Annulla il tuo piano a pagamento in Crediti e piano prima di eliminare l'account.",
    ),
    "This user is already a member of the workspace.": (
        "Cette personne est déjà membre de l'espace de travail.",
        "Questa persona è già membro dello spazio di lavoro.",
    ),
    "Choose who takes over their threads and scheduled posts.": (
        "Choisissez qui reprend ses discussions et ses publications programmées.",
        "Scegli chi prende in carico le sue discussioni e i post programmati.",
    ),
    "This template is not available yet.": (
        "Ce modèle n'est pas encore disponible.",
        "Questo modello non è ancora disponibile.",
    ),
    "This post was not generated, so there are no settings to regenerate from.": (
        "Cette publication n'a pas été générée : il n'y a pas de réglages à partir desquels la régénérer.",
        "Questo post non è stato generato, quindi non ci sono impostazioni da cui rigenerarlo.",
    ),
    "This post has no slot to clear.": (
        "Cette publication n'a pas de créneau à libérer.",
        "Questo post non ha uno slot da liberare.",
    ),
    "This post can no longer be cancelled.": (
        "Cette publication ne peut plus être annulée.",
        "Questo post non può più essere annullato.",
    ),
    "That generation has not finished.": (
        "Cette génération n'est pas terminée.",
        "Questa generazione non è terminata.",
    ),
    "Unschedule this post before replacing its content.": (
        "Déplanifiez cette publication avant d'en remplacer le contenu.",
        "Rimuovi il post dalla programmazione prima di sostituirne il contenuto.",
    ),
    "An import of this workspace's website is already running.": (
        "Un import du site de cet espace de travail est déjà en cours.",
        "È già in corso un'importazione del sito di questo spazio di lavoro.",
    ),
    "This import has not finished reading the website.": (
        "Cet import n'a pas fini de lire le site.",
        "Questa importazione non ha finito di leggere il sito.",
    ),
    "Review every section before applying the import.": (
        "Relisez chaque section avant d'appliquer l'import.",
        "Rivedi ogni sezione prima di applicare l'importazione.",
    ),
    "This campaign is closed. Its brief and plan are kept as they were.": (
        "Cette campagne est close. Son brief et son plan sont conservés tels quels.",
        "Questa campagna è chiusa. Il brief e il piano restano com'erano.",
    ),
    "The price changed since you confirmed it. Review the new estimate.": (
        "Le prix a changé depuis votre confirmation. Vérifiez la nouvelle estimation.",
        "Il prezzo è cambiato da quando l'hai confermato. Controlla la nuova stima.",
    ),
    "This render cannot be changed that way now.": (
        "Ce rendu ne peut plus être modifié de cette façon.",
        "Questo render non può più essere modificato così.",
    ),
    "A plan is already being written for this campaign.": (
        "Un plan est déjà en cours de rédaction pour cette campagne.",
        "Si sta già scrivendo un piano per questa campagna.",
    ),
    "Generate a plan before seeding the calendar.": (
        "Générez un plan avant de remplir le calendrier.",
        "Genera un piano prima di popolare il calendario.",
    ),
    "Benchmark participation terms have not been published yet.": (
        "Les conditions de participation aux comparatifs ne sont pas encore publiées.",
        "Le condizioni di partecipazione ai benchmark non sono ancora pubblicate.",
    ),
    "Those terms have been replaced. Review the current version first.": (
        "Ces conditions ont été remplacées. Lisez d'abord la version actuelle.",
        "Queste condizioni sono state sostituite. Leggi prima la versione attuale.",
    ),
    "This workspace needs a category before it can join a benchmark cohort.": (
        "Cet espace de travail doit avoir une catégorie pour rejoindre un comparatif.",
        "Questo spazio di lavoro deve avere una categoria per unirsi a un benchmark.",
    ),
    "This workspace has not opted into benchmarks.": (
        "Cet espace de travail ne participe pas aux comparatifs.",
        "Questo spazio di lavoro non partecipa ai benchmark.",
    ),
    "Benchmarks are available to workspaces that contribute to them.": (
        "Les comparatifs sont réservés aux espaces de travail qui y contribuent.",
        "I benchmark sono disponibili per gli spazi di lavoro che vi contribuiscono.",
    ),
    "A real person needs recorded consent, and it must still stand.": (
        "Une personne réelle doit avoir donné un consentement enregistré, toujours valable.",
        "Una persona reale deve aver dato un consenso registrato e ancora valido.",
    ),
    "These portraits are not ready yet.": (
        "Ces portraits ne sont pas encore prêts.",
        "Questi ritratti non sono ancora pronti.",
    ),
    "A pre-publish check blocks this post. Fix it before approving or scheduling.": (
        "Une vérification avant publication bloque cette publication. Corrigez-la avant de valider ou de programmer.",
        "Un controllo prima della pubblicazione blocca questo post. Correggilo prima di approvarlo o programmarlo.",
    ),
    "An upstream provider could not complete this request.": (
        "Un fournisseur externe n'a pas pu traiter cette demande.",
        "Un fornitore esterno non è riuscito a completare la richiesta.",
    ),
    "The publishing provider could not complete this request.": (
        "Le service de publication n'a pas pu traiter cette demande.",
        "Il servizio di pubblicazione non è riuscito a completare la richiesta.",
    ),
    "The social provider is not ready for this operation.": (
        "Le service des réseaux sociaux n'est pas prêt pour cette opération.",
        "Il servizio social non è pronto per questa operazione.",
    ),
    "A reference picture could not be read.": (
        "Une image de référence n'a pas pu être lue.",
        "Non è stato possibile leggere un'immagine di riferimento.",
    ),
    "A trend source could not be read.": (
        "Une source de tendances n'a pas pu être lue.",
        "Non è stato possibile leggere una fonte di tendenze.",
    ),
    "Trend harvesting is yielding to higher-priority traffic.": (
        "La collecte des tendances laisse passer un trafic prioritaire.",
        "La raccolta delle tendenze cede il passo a traffico prioritario.",
    ),
    "The measurement provider could not complete this request.": (
        "Le service de mesure n'a pas pu traiter cette demande.",
        "Il servizio di misurazione non è riuscito a completare la richiesta.",
    ),
    "This provider cannot report that on this platform.": (
        "Ce service ne peut pas fournir cette donnée sur cette plateforme.",
        "Questo servizio non può fornire questo dato su questa piattaforma.",
    ),
    "That photo could not be read from the product page.": (
        "Cette photo n'a pas pu être lue sur la page produit.",
        "Non è stato possibile leggere questa foto dalla pagina prodotto.",
    ),
    "A trend provider could not be read.": (
        "Un fournisseur de tendances n'a pas pu être lu.",
        "Non è stato possibile leggere un fornitore di tendenze.",
    ),
    "The social provider could not complete this request.": (
        "Le service des réseaux sociaux n'a pas pu traiter cette demande.",
        "Il servizio social non è riuscito a completare la richiesta.",
    ),
    "Too many requests.": ("Trop de demandes.", "Troppe richieste."),
    "This sign-in is not for this workspace, or it took too long. Start it again.": (
        "Cette connexion ne correspond pas à cet espace de travail, ou elle a pris trop de temps. Recommencez.",
        "Questo accesso non è per questo spazio di lavoro, oppure ha richiesto troppo tempo. Ricomincia.",
    ),
    "We could not check that you are human. Try again.": (
        "Nous n'avons pas pu vérifier que vous êtes humain. Réessayez.",
        "Non siamo riusciti a verificare che tu sia una persona. Riprova.",
    ),
    "That is not your current password.": (
        "Ce n'est pas votre mot de passe actuel.",
        "Questa non è la tua password attuale.",
    ),
    "Choose a stronger password.": (
        "Choisissez un mot de passe plus robuste.",
        "Scegli una password più sicura.",
    ),
    "Another account already uses that email.": (
        "Un autre compte utilise déjà cette adresse e-mail.",
        "Un altro account usa già questo indirizzo email.",
    ),
    "That is already your email.": (
        "C'est déjà votre adresse e-mail.",
        "È già il tuo indirizzo email.",
    ),
    "This link is invalid, expired or was cancelled.": (
        "Ce lien est invalide, expiré ou a été annulé.",
        "Questo link non è valido, è scaduto o è stato annullato.",
    ),
    "Type DELETE followed by your email, exactly.": (
        "Tapez DELETE suivi de votre adresse e-mail, exactement.",
        "Scrivi DELETE seguito dal tuo indirizzo email, esattamente.",
    ),
    "This role cannot be assigned through this endpoint.": (
        "Ce rôle ne peut pas être attribué de cette façon.",
        "Questo ruolo non può essere assegnato in questo modo.",
    ),
    "No ComposeVision account exists with this email yet.": (
        "Aucun compte ComposeVision n'existe encore avec cette adresse e-mail.",
        "Non esiste ancora un account ComposeVision con questo indirizzo email.",
    ),
    "The workspace owner's membership cannot be changed here.": (
        "L'adhésion du propriétaire de l'espace de travail ne peut pas être modifiée ici.",
        "L'appartenenza del proprietario dello spazio di lavoro non può essere modificata qui.",
    ),
    "Pick another member of this workspace.": (
        "Choisissez un autre membre de cet espace de travail.",
        "Scegli un altro membro di questo spazio di lavoro.",
    ),
    "Payments are not configured on this server.": (
        "Les paiements ne sont pas configurés sur ce serveur.",
        "I pagamenti non sono configurati su questo server.",
    ),
    "That range does not exist in this post.": (
        "Ce passage n'existe pas dans cette publication.",
        "Questo intervallo non esiste in questo post.",
    ),
    "A reaction is a single emoji.": (
        "Une réaction est un seul emoji.",
        "Una reazione è una sola emoji.",
    ),
    "This post has no revision with that number.": (
        "Cette publication n'a pas de version portant ce numéro.",
        "Questo post non ha una revisione con quel numero.",
    ),
    "This post cannot be edited in its current state.": (
        "Cette publication ne peut pas être modifiée dans son état actuel.",
        "Questo post non può essere modificato nel suo stato attuale.",
    ),
    "That media asset is not attached to this post.": (
        "Ce média n'est pas joint à cette publication.",
        "Questo contenuto multimediale non è allegato a questo post.",
    ),
    "These platform options are not valid.": (
        "Ces options de plateforme ne sont pas valides.",
        "Queste opzioni della piattaforma non sono valide.",
    ),
    "This post has no target on that platform.": (
        "Cette publication ne cible pas cette plateforme.",
        "Questo post non ha una destinazione su quella piattaforma.",
    ),
    "That kind of template is not available yet.": (
        "Ce type de modèle n'est pas encore disponible.",
        "Questo tipo di modello non è ancora disponibile.",
    ),
    "This template's platform options are not valid.": (
        "Les options de plateforme de ce modèle ne sont pas valides.",
        "Le opzioni della piattaforma di questo modello non sono valide.",
    ),
    "This template's products, accounts or modes are not valid.": (
        "Les produits, comptes ou modes de ce modèle ne sont pas valides.",
        "I prodotti, gli account o le modalità di questo modello non sono validi.",
    ),
    "This template's text uses a placeholder that does not exist.": (
        "Le texte de ce modèle utilise un champ variable qui n'existe pas.",
        "Il testo di questo modello usa un segnaposto che non esiste.",
    ),
    "A placeholder is filled from a product; choose at least one.": (
        "Un champ variable se remplit à partir d'un produit : choisissez-en au moins un.",
        "Un segnaposto si compila da un prodotto: scegline almeno uno.",
    ),
    "This recurrence rule cannot be used.": (
        "Cette règle de récurrence ne peut pas être utilisée.",
        "Questa regola di ricorrenza non può essere usata.",
    ),
    "This file cannot be used as media.": (
        "Ce fichier ne peut pas servir de média.",
        "Questo file non può essere usato come contenuto multimediale.",
    ),
    "This edit cannot be applied to that media.": (
        "Cette modification ne peut pas s'appliquer à ce média.",
        "Questa modifica non può essere applicata a questo contenuto.",
    ),
    "No media editor is configured for this deployment.": (
        "Aucun éditeur de médias n'est configuré sur ce déploiement.",
        "Nessun editor multimediale è configurato in questa installazione.",
    ),
    "This picture cannot be used as a brand asset.": (
        "Cette image ne peut pas servir d'élément de marque.",
        "Questa immagine non può essere usata come elemento del brand.",
    ),
    # S20 — the Studio storyboard.
    "Scene-by-scene editing is not available yet.": (
        "La modification scène par scène n'est pas encore disponible.",
        "La modifica scena per scena non è ancora disponibile.",
    ),
    "This storyboard cannot be made as it is.": (
        "Ce storyboard ne peut pas être réalisé tel quel.",
        "Questo storyboard non può essere realizzato così com'è.",
    ),
    "This scene needs something your package's video model cannot do.": (
        "Cette scène demande quelque chose que le modèle vidéo de votre offre ne sait pas faire.",
        "Questa scena richiede qualcosa che il modello video del tuo piano non sa fare.",
    ),
    "This template cannot be made this way.": (
        "Ce modèle ne peut pas être réalisé ainsi.",
        "Questo modello non può essere realizzato così.",
    ),
    "This template needs the offer: what it is and the day it ends.": (
        "Ce modèle a besoin de l'offre : ce qu'elle est et le jour où elle se termine.",
        "Questo modello ha bisogno dell'offerta: cos'è e il giorno in cui finisce.",
    ),
    "That offer has already ended.": (
        "Cette offre est déjà terminée.",
        "Questa offerta è già terminata.",
    ),
    "The photo could not be read into a card. No credits were taken.": (
        "La photo n'a pas pu être transformée en carte. Aucun crédit n'a été pris.",
        "Non è stato possibile trasformare la foto in una scheda. Nessun credito è stato scalato.",
    ),
    "No credit cost is configured for this generation.": (
        "Aucun coût en crédits n'est configuré pour cette génération.",
        "Nessun costo in crediti è configurato per questa generazione.",
    ),
    "This product needs at least one reference image before it can generate.": (
        "Ce produit doit avoir au moins une image de référence avant de pouvoir générer.",
        "Questo prodotto ha bisogno di almeno un'immagine di riferimento prima di generare.",
    ),
    "This generation kind is not available yet.": (
        "Ce type de génération n'est pas encore disponible.",
        "Questo tipo di generazione non è ancora disponibile.",
    ),
    "This generation mode is not available yet.": (
        "Ce mode de génération n'est pas encore disponible.",
        "Questa modalità di generazione non è ancora disponibile.",
    ),
    "This composition cannot be made.": (
        "Cette composition ne peut pas être réalisée.",
        "Questa composizione non può essere realizzata.",
    ),
    "Connect at least one account before scheduling an auto-publish post.": (
        "Connectez au moins un compte avant de programmer une publication automatique.",
        "Collega almeno un account prima di programmare un post a pubblicazione automatica.",
    ),
    "This post's media does not make a post on every platform it goes to.": (
        "Les médias de cette publication ne conviennent pas à toutes ses plateformes.",
        "I contenuti di questo post non vanno bene per tutte le piattaforme a cui è destinato.",
    ),
    "That variant cannot be selected.": (
        "Cette variante ne peut pas être choisie.",
        "Questa variante non può essere scelta.",
    ),
    "Enter a website like yourbrand.com.": (
        "Saisissez un site comme votremarque.com.",
        "Inserisci un sito come tuobrand.com.",
    ),
    "Paste the full link to the product page, like yourshop.com/products/…": (
        "Collez le lien complet de la page produit, comme votreboutique.com/products/…",
        "Incolla il link completo della pagina prodotto, come tuonegozio.com/products/…",
    ),
    "That section's value is not valid.": (
        "La valeur de cette section n'est pas valide.",
        "Il valore di questa sezione non è valido.",
    ),
    "Video rendering is not available on this deployment.": (
        "Le rendu vidéo n'est pas disponible sur ce déploiement.",
        "Il rendering video non è disponibile in questa installazione.",
    ),
    "A price is missing from the video catalogue.": (
        "Un prix manque dans le catalogue vidéo.",
        "Manca un prezzo nel catalogo video.",
    ),
    "The brief is not finished yet.": (
        "Le brief n'est pas encore terminé.",
        "Il brief non è ancora finito.",
    ),
    "No price is configured for a campaign plan.": (
        "Aucun prix n'est configuré pour un plan de campagne.",
        "Nessun prezzo è configurato per un piano di campagna.",
    ),
    "This draft cannot be acted on.": (
        "Aucune action n'est possible sur ce brouillon.",
        "Non è possibile agire su questa bozza.",
    ),
    "Only a succeeded generation can be revised.": (
        "Seule une génération réussie peut être révisée.",
        "Solo una generazione riuscita può essere rivista.",
    ),
    "The platform does not allow this on this comment.": (
        "La plateforme ne permet pas cela sur ce commentaire.",
        "La piattaforma non lo consente su questo commento.",
    ),
    "This avatar cannot be used this way.": (
        "Cet avatar ne peut pas être utilisé ainsi.",
        "Questo avatar non può essere usato così.",
    ),
    "A reference is no longer available.": (
        "Une référence n'est plus disponible.",
        "Un riferimento non è più disponibile.",
    ),
}

_COLUMN = {"fr": 0, "it": 1}


def in_language(message: str) -> str:
    """`message` in the active language when it has a translation here."""
    column = _COLUMN.get((translation.get_language() or "")[:2])
    words = ERRORS.get(message)
    return message if column is None or words is None else words[column]
