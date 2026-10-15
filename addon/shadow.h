/*
 * Shadow: the text before the cursor in a field whose app does not report it.
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <chrono>
#include <string>

#include <fcitx-utils/rect.h>
#include <fcitx-utils/utf8.h>
#include <fcitx/inputcontext.h>

namespace fcitx {

// The text before the cursor in a field whose app does not report it (terminals: Konsole deliberately
// doesn't, see invent.kde.org/utilities/konsole/-/merge_requests/800), reconstructed from what we committed
// there and the keys typed since. fcitx5 cannot tell fields apart within one input context, so "the same
// field" means an unbroken stretch: the shadow dies on focus out, on any key that can move the cursor or
// end the line, and when the app's cursor moves while we didn't cause it (a click). A cursor move within
// a second of our own change is the app redrawing after it, so it is followed rather than fatal.
class Shadow {
public:
    using Clock = std::chrono::steady_clock;
    static constexpr size_t keep = 400;  // characters; only the tail matters for spacing and casing
    static constexpr auto grace = std::chrono::seconds(1);

    // Start tracking `ic` from an unknown point: the text before our first commit is not known.
    void reset(const ICUUID &ic, const Rect &cursor, Clock::time_point now) {
        ic_ = ic;
        cursor_ = cursor;
        text_.clear();
        valid_ = true;
        touch(now);
    }

    void invalidate() { valid_ = false; }

    bool tracks(const ICUUID &ic) const { return valid_ && ic_ == ic; }

    // Whether the shadow describes this field right now: same input context, cursor where we left it.
    bool usable(const ICUUID &ic, const Rect &cursor) const { return tracks(ic) && cursor == cursor_; }

    const std::string &text() const { return text_; }

    void append(const std::string &text, Clock::time_point now) {
        text_ += text;
        const size_t length = utf8::lengthValidated(text_);
        if (length == utf8::INVALID_LENGTH) {
            invalidate();
            return;
        }
        if (length > keep) {
            text_.erase(text_.begin(), utf8::nextNChar(text_.begin(), length - keep));
        }
        touch(now);
    }

    // Backspace removes the last character; past what we know of, the shadow knows nothing.
    void backspace(Clock::time_point now) {
        if (text_.empty()) {
            invalidate();
            return;
        }
        const size_t length = utf8::lengthValidated(text_);
        if (length == utf8::INVALID_LENGTH) {
            invalidate();
            return;
        }
        text_.erase(utf8::nextNChar(text_.begin(), length - 1), text_.end());
        touch(now);
    }

    // The app reported a new cursor position. `ours` is true during a dictation, when every change is ours.
    void cursorMoved(const Rect &cursor, bool ours, Clock::time_point now) {
        if (ours || now - lastChange_ <= grace) {
            cursor_ = cursor;
        } else {
            invalidate();
        }
    }

    void touch(Clock::time_point now) { lastChange_ = now; }

private:
    ICUUID ic_{};
    Rect cursor_;
    std::string text_;
    bool valid_ = false;
    Clock::time_point lastChange_{};
};

}  // namespace fcitx
