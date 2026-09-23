//! Deciding what the interviewer said: is this a question, is it finished,
//! and does the next thing they say belong to it.
//!
//! Audio is cut into utterances after 360 ms of quiet so transcription starts
//! the moment the interviewer pauses. That is what keeps answers fast, but a
//! 360 ms pause is also an ordinary breath in the middle of a sentence:
//! "Tell me about a time when you… had to push back on a manager." arrives as
//! two utterances. Answering the first half alone is the failure this module
//! exists to prevent, without slowing down the common case of a question
//! asked in one go:
//!
//! * a question that already reads as finished is answered immediately;
//! * one that ends mid-sentence ("…when you", "…about the") is held briefly
//!   for the rest of it, and held longer while the interviewer is audibly
//!   still talking;
//! * speech that starts again within `MERGE_GAP_MS` of the question is merged
//!   into it and the answer is rewritten for the whole question — unless it
//!   is only a backchannel ("mm-hmm", "take your time"), which must never
//!   replace an answer the candidate is already reading.

/// Speech that resumes within this long after the question's last word is
/// treated as the rest of the same question.
pub const MERGE_GAP_MS: u64 = 2_000;
/// How long a question that ends mid-sentence waits for its continuation
/// before being answered as-is.
pub const FRAGMENT_HOLD_MS: u64 = 1_200;
/// Each sign of live speech pushes that wait out by this much, so a long
/// continuation still being spoken is waited for rather than raced.
pub const VOICE_EXTEND_MS: u64 = 1_500;
/// Upper bound on any wait, so a stuck signal can never swallow a question.
pub const MAX_FRAGMENT_HOLD_MS: u64 = 8_000;

/// Words an interviewer opens with before the actual ask: "Okay, so tell me
/// about…" must be recognized as "tell me about…".
const LEADING_FILLERS: &[&str] = &[
    "okay",
    "ok",
    "so",
    "alright",
    "right",
    "and",
    "well",
    "great",
    "cool",
    "perfect",
    "nice",
    "awesome",
    "now",
    "next",
    "um",
    "uh",
    "hmm",
    "yeah",
    "yes",
    "sure",
    "thanks",
    "good",
    "excellent",
    "interesting",
    "fantastic",
    "got",
    "it",
    "then",
    // Acknowledgement sounds, which Whisper spells several ways.
    "mm-hmm",
    "mhm",
    "uh-huh",
    "mm",
    "mmm",
    "yep",
    "yup",
    "oh",
    "ah",
    "sorry",
];

/// A sentence starting with one of these, after fillers, is a question.
const QUESTION_STARTERS: &[&str] = &[
    "what",
    "why",
    "how",
    "when",
    "where",
    "who",
    "whom",
    "whose",
    "which",
    "tell me",
    "tell us",
    "describe",
    "explain",
    "walk me",
    "walk us",
    "talk me",
    "talk us",
    "take me",
    "give me",
    "give us",
    "can you",
    "could you",
    "would you",
    "will you",
    "do you",
    "did you",
    "have you",
    "are you",
    "is there",
    "what's",
    "how's",
    "who's",
    "where's",
];

/// Requests that make an utterance a question wherever they appear, so a
/// statement followed by the ask ("We use Kafka a lot. I'd love to hear how
/// you've used it") is still caught. Deliberately multi-word: a single word
/// like "explain" anywhere would also fire on "I'll explain the process".
const REQUEST_PHRASES: &[&str] = &[
    "tell me about",
    "tell us about",
    "walk me through",
    "walk us through",
    "talk me through",
    "take me through",
    "i'd like to hear",
    "i would like to hear",
    "i'd love to hear",
    "i want to hear",
    "curious to hear",
    "curious about how",
    "give me an example",
    "share an example",
    "share a time",
    "what's your",
    "what is your",
];

/// Call logistics, not interview questions — answering "can you hear me"
/// with a STAR story is worse than saying nothing.
const LOGISTICS: &[&str] = &[
    "can you hear me",
    "can you see me",
    "can you see my screen",
    "can everyone see",
    "you're on mute",
    "you are on mute",
    "you're muted",
    "are you able to hear",
    "is my audio",
];

/// A question ending on one of these has not finished.
const TRAILING_CONTINUATIONS: &[&str] = &[
    "and", "or", "but", "so", "because", "with", "the", "a", "an", "to", "of", "for", "in", "on",
    "at", "about", "when", "where", "which", "that", "your", "you", "my", "our", "their", "um",
    "uh", "like", "if", "how", "what", "why", "is", "are", "was", "were", "do", "did", "have",
    "this", "any", "some", "from", "as",
];

/// Short acknowledgements, and Whisper's known hallucinations on near-silent
/// audio, that must not merge into (and so replace) the current answer.
const BACKCHANNELS: &[&str] = &[
    "take your time",
    "no rush",
    "go ahead",
    "go for it",
    "sounds good",
    "that's fine",
    "no problem",
    "no worries",
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "makes sense",
    "i see",
    "got it",
    "of course",
];

fn normalize(text: &str) -> String {
    text.to_lowercase()
        .replace(['\u{2019}', '\u{2018}'], "'")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn words(text: &str) -> Vec<String> {
    normalize(text)
        .split(' ')
        .map(|w| {
            w.trim_matches(|c: char| !c.is_alphanumeric() && c != '\'')
                .to_string()
        })
        .filter(|w| !w.is_empty())
        .collect()
}

/// The sentence with any leading fillers removed, as plain lowercase words.
fn strip_fillers(sentence: &str) -> String {
    let words = words(sentence);
    let start = words
        .iter()
        .position(|w| !LEADING_FILLERS.contains(&w.as_str()))
        .unwrap_or(words.len());
    words[start..].join(" ")
}

pub fn looks_like_question(text: &str) -> bool {
    let normalized = normalize(text);
    if normalized.is_empty() || LOGISTICS.iter().any(|p| normalized.contains(p)) {
        return false;
    }
    if normalized.trim_end().ends_with('?') {
        return true;
    }
    if REQUEST_PHRASES.iter().any(|p| normalized.contains(p)) {
        return true;
    }
    normalized
        .split(['.', '?', '!', ';'])
        .map(strip_fillers)
        .any(|sentence| {
            QUESTION_STARTERS
                .iter()
                .any(|starter| sentence == *starter || sentence.starts_with(&format!("{starter} ")))
        })
}

/// True when the question stops mid-sentence. A question mark is trusted;
/// a full stop is not — Whisper adds one at every pause it cuts on, so
/// "…a time when you." still ends on "you".
pub fn is_fragment(text: &str) -> bool {
    let normalized = normalize(text);
    let trimmed = normalized.trim_end();
    if trimmed.is_empty() {
        return false;
    }
    if trimmed.ends_with('?') {
        return false;
    }
    if trimmed.ends_with(',') || trimmed.ends_with("...") || trimmed.ends_with('…') {
        return true;
    }
    words(trimmed)
        .last()
        .is_some_and(|last| TRAILING_CONTINUATIONS.contains(&last.as_str()))
}

/// Speech that carries no new content for the question it follows.
pub fn is_backchannel(text: &str) -> bool {
    let content = strip_fillers(text);
    if content.split(' ').filter(|w| !w.is_empty()).count() <= 2 {
        // "Mm-hmm", "Yeah, okay", "Right." — and a lone filler strips to "".
        return true;
    }
    BACKCHANNELS
        .iter()
        .any(|b| content == *b || content.starts_with(&format!("{b} ")))
}

/// The two halves as one question, keeping the second half's punctuation.
/// A full stop Whisper put at the pause is removed so the joined question
/// reads as the one sentence the interviewer actually said.
pub fn merge(first: &str, rest: &str) -> String {
    let first = first.trim_end();
    let first = if is_fragment(first) {
        first.trim_end_matches(['.', ',', '…'])
    } else {
        first
    };
    let rest = rest.trim();
    if first.is_empty() {
        return rest.to_string();
    }
    if rest.is_empty() {
        return first.to_string();
    }
    let rest = if first.ends_with(['.', '?', '!']) {
        rest.to_string()
    } else {
        // Continuing the same sentence: "…when you had to…", not "…when you Had to…".
        let mut chars = rest.chars();
        match chars.next() {
            Some(c) if c.is_uppercase() && !rest.starts_with("I ") && !rest.starts_with("I'") => {
                c.to_lowercase().chain(chars).collect()
            }
            _ => rest.to_string(),
        }
    };
    format!("{first} {rest}")
}

/// What to do with a question right now.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Readiness {
    AnswerNow,
    /// Wait this long (ms) for the rest of it.
    Hold(u64),
}

pub fn readiness(question: &str) -> Readiness {
    if is_fragment(question) {
        Readiness::Hold(FRAGMENT_HOLD_MS)
    } else {
        Readiness::AnswerNow
    }
}

/// Whether speech that started `gap_ms` after the question's last word
/// belongs to that question.
pub fn continues_question(gap_ms: u64, text: &str) -> bool {
    gap_ms <= MERGE_GAP_MS && !is_backchannel(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn direct_questions_and_requests_are_detected_without_punctuation() {
        assert!(looks_like_question("Tell me about a difficult project"));
        assert!(looks_like_question("How did you resolve the conflict"));
        assert!(looks_like_question("Walk me through your resume"));
        assert!(looks_like_question("What is a CI/CD pipeline"));
        assert!(looks_like_question(
            "What's the difference between a process and a thread"
        ));
    }

    #[test]
    fn a_question_after_opening_fillers_is_still_a_question() {
        // The most common way a real interviewer starts, and the old
        // prefix-only check missed every one of these.
        assert!(looks_like_question("Okay, so tell me about yourself."));
        assert!(looks_like_question(
            "Alright. Great. What's your biggest weakness."
        ));
        assert!(looks_like_question(
            "Um, so how would you design a rate limiter"
        ));
        assert!(looks_like_question(
            "Perfect, thanks. Next, describe a time you failed."
        ));
        assert!(looks_like_question(
            "Sorry, what was the scale of that system"
        ));
    }

    #[test]
    fn an_ask_after_a_statement_is_detected() {
        assert!(looks_like_question(
            "We use Kafka a lot here. I'd love to hear how you've used it."
        ));
        assert!(looks_like_question(
            "Our team is remote. How do you stay aligned with others"
        ));
        assert!(looks_like_question(
            "I see you led a migration. Tell me about that."
        ));
    }

    #[test]
    fn statements_small_talk_and_call_logistics_are_not_questions() {
        assert!(!looks_like_question("Thanks, that is all"));
        assert!(!looks_like_question("Okay great."));
        assert!(!looks_like_question("I'll explain the process next week."));
        assert!(!looks_like_question("Let me share my screen."));
        assert!(!looks_like_question("Share your screen when you're ready."));
        assert!(!looks_like_question("Can you hear me?"));
        assert!(!looks_like_question("Sorry, you're on mute."));
        assert!(!looks_like_question(""));
    }

    #[test]
    fn a_sentence_cut_at_a_pause_is_a_fragment_even_with_a_full_stop() {
        assert!(is_fragment("Tell me about a time when you"));
        assert!(is_fragment("Tell me about a time when you."));
        assert!(is_fragment("What is your experience with"));
        assert!(is_fragment("How would you design the,"));
        assert!(is_fragment("So walk me through the…"));
    }

    #[test]
    fn a_finished_question_is_not_a_fragment() {
        assert!(!is_fragment("Tell me about yourself."));
        assert!(!is_fragment("What about you?"));
        assert!(!is_fragment("Walk me through your resume"));
        assert!(!is_fragment("How do you handle conflict on a team?"));
        assert!(!is_fragment(""));
    }

    #[test]
    fn acknowledgements_and_whisper_hallucinations_are_backchannels() {
        for text in [
            "Mm-hmm.",
            "Yeah.",
            "Okay, right.",
            "Take your time.",
            "No rush, go ahead.",
            "Thank you.",
            "Thanks for watching!",
            "Sure, sounds good.",
            "Mm-hmm, take your time.",
            "Uh-huh, go ahead.",
        ] {
            assert!(is_backchannel(text), "{text}");
        }
    }

    #[test]
    fn real_continuations_are_not_backchannels() {
        for text in [
            "had to push back on a manager.",
            "And what did you learn from it?",
            "the hardest bug you've ever fixed.",
        ] {
            assert!(!is_backchannel(text), "{text}");
        }
    }

    #[test]
    fn two_halves_merge_into_the_sentence_that_was_actually_said() {
        assert_eq!(
            merge(
                "Tell me about a time when you.",
                "Had to push back on a manager."
            ),
            "Tell me about a time when you had to push back on a manager."
        );
        assert_eq!(
            merge("What's your name?", "And where did you study?"),
            "What's your name? And where did you study?"
        );
        // "I" stays capitalized when the continuation starts with it.
        assert_eq!(
            merge("So what would you do if", "I asked you to rewrite it?"),
            "So what would you do if I asked you to rewrite it?"
        );
        assert_eq!(
            merge("", "Tell me about yourself."),
            "Tell me about yourself."
        );
    }

    #[test]
    fn finished_questions_are_answered_now_and_fragments_are_held() {
        assert_eq!(readiness("Tell me about yourself."), Readiness::AnswerNow);
        assert_eq!(
            readiness("Tell me about a time when you."),
            Readiness::Hold(FRAGMENT_HOLD_MS)
        );
    }

    #[test]
    fn only_prompt_substantive_speech_continues_a_question() {
        assert!(continues_question(900, "had to push back on a manager."));
        assert!(!continues_question(900, "Mm-hmm."));
        assert!(!continues_question(
            MERGE_GAP_MS + 1,
            "had to push back on a manager."
        ));
    }
}
