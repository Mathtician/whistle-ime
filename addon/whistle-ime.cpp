/*
 * whistle-ime: dictation into the focused text field, with a live preview.
 *
 * The addon is thin. It keeps one backend process running (the speech model stays loaded), turns the
 * trigger key into START/STOP, shows the backend's PREEDIT as inline preedit and inserts its COMMITs.
 * The backend owns every decision about text, including spacing; see src/whistle_ime/backend.py for the
 * protocol. Structure and several hard-won details follow fcitx5-voiceinput (MIT,
 * github.com/GavinLiuOnline/fcitx5-voiceinput).
 *
 * Session states:
 *   Idle -> Recording        trigger key
 *   Recording -> Finishing   trigger key or Escape (STOP); any other key also STOPs, and is replayed after
 *                            the final commit so it lands after the dictated text
 *   Recording/Finishing -> Cancelling
 *                            focus left the field (CANCEL); its preedit is gone and it can no longer
 *                            receive commits, so everything until DONE is dropped
 *   any -> Idle              DONE, or the backend died
 *
 * SPDX-License-Identifier: MIT
 */
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include <fcitx-config/iniparser.h>
#include <fcitx-config/option.h>
#include <fcitx-utils/capabilityflags.h>
#include <fcitx-utils/event.h>
#include <fcitx-utils/i18n.h>
#include <fcitx-utils/key.h>
#include <fcitx-utils/log.h>
#include <fcitx-utils/utf8.h>
#include <fcitx/addonfactory.h>
#include <fcitx/addoninstance.h>
#include <fcitx/addonmanager.h>
#include <fcitx/event.h>
#include <fcitx/inputcontext.h>
#include <fcitx/inputcontextmanager.h>
#include <fcitx/inputpanel.h>
#include <fcitx/instance.h>
#include <fcitx/text.h>
#include <fcitx/userinterface.h>

extern char **environ;

namespace fcitx {

FCITX_DEFINE_LOG_CATEGORY(whistle_ime, "whistle-ime");
#define WHISTLE_INFO() FCITX_LOGC(::fcitx::whistle_ime, Info)

FCITX_CONFIGURATION(
    WhistleImeConfig,
    // Super keeps the chord clear of app shortcuts; Super+V alone is Plasma's clipboard popup.
    KeyListOption triggerKey{this, "TriggerKey", "Trigger Key", {Key("Super+Alt+V")}, KeyListConstrain()};
    // Run with /bin/sh -c; it must speak the protocol on stdin/stdout.
    Option<std::string> backend{this, "Backend", "Backend Command", ""};
    // Logs what the app reports about the field (including the text around the cursor) at START and DONE.
    Option<bool> logContext{this, "LogContext", "Log Field Context", false};);

class WhistleIme final : public AddonInstance {
public:
    explicit WhistleIme(Instance *instance) : instance_(instance) {
        readAsIni(config_, "conf/whistle-ime.conf");
        spawnBackend();

        keyHandler_ = instance_->watchEvent(
            EventType::InputContextKeyEvent, EventWatcherPhase::PreInputMethod,
            [this](Event &event) { onKey(static_cast<KeyEvent &>(event)); });

        focusHandler_ = instance_->watchEvent(
            EventType::InputContextFocusOut, EventWatcherPhase::Default, [this](Event &event) {
                auto &focus = static_cast<FocusOutEvent &>(event);
                if (focus.inputContext()->uuid() == icUuid_ &&
                    (state_ == State::Recording || state_ == State::Finishing)) {
                    WHISTLE_INFO() << "focus left the field: cancelling";
                    send("CANCEL");
                    endSession(State::Cancelling);
                }
            });
    }

    ~WhistleIme() override { stopBackend(); }

    void reloadConfig() override { readAsIni(config_, "conf/whistle-ime.conf"); }
    const Configuration *getConfig() const override { return &config_; }
    void setConfig(const RawConfig &raw) override {
        config_.load(raw);
        safeSaveAsIni(config_, "conf/whistle-ime.conf");
    }

private:
    enum class State { Idle, Recording, Finishing, Cancelling };

    // ---- keys -------------------------------------------------------------------------------------

    void onKey(KeyEvent &event) {
        if (event.isRelease()) {
            return;
        }
        const Key &key = event.key();
        const bool trigger = key.checkKeyList(config_.triggerKey.value());
        switch (state_) {
        case State::Idle:
            if (trigger) {
                start(event.inputContext());
                event.filterAndAccept();
            }
            return;
        case State::Recording:
            if (trigger || key.check(FcitxKey_Escape)) {
                stop();
                event.filterAndAccept();
            } else if (!key.isModifier()) {
                // Stop, then type: the key is replayed once the dictated text is in.
                held_.push_back(key);
                stop();
                event.filterAndAccept();
            }
            return;
        case State::Finishing:
            if (!key.isModifier() && !trigger && !key.check(FcitxKey_Escape)) {
                held_.push_back(key);
            }
            event.filterAndAccept();
            return;
        case State::Cancelling:
            if (trigger) {
                event.filterAndAccept();  // too soon to start again; don't leak the chord to the app
            }
            return;  // other keys belong to wherever focus went
        }
    }

    void start(InputContext *ic) {
        if (backendFd_ < 0) {
            spawnBackend();  // it died earlier; this press brings it back but cannot use it yet
        }
        if (!ready_) {
            showStatus(ic, "Whistle is loading…");
            return;
        }
        icUuid_ = ic->uuid();
        held_.clear();
        errorShown_ = false;
        // Ask apps that hide preedit unless asked (e.g. GTK over XIM) to show it for this session.
        savedCaps_ = ic->capabilityFlags();
        if (!savedCaps_.test(CapabilityFlag::Preedit)) {
            ic->setCapabilityFlags(savedCaps_ | CapabilityFlag::Preedit);
        }
        state_ = State::Recording;
        logContext(ic, "START");
        send("START\t" + context(ic));
        showStatus(ic, "🎤 Dictating");
    }

    void stop() {
        state_ = State::Finishing;
        send("STOP");
        showStatus(findIc(), "Finishing…");
    }

    // START's context fields: "1<TAB><text before the cursor>" when the app reports it, else "0<TAB>". The
    // backend decides spacing and casing from it. Only the last 200 characters are sent, escaped so they fit
    // on one line.
    static std::string context(InputContext *ic) {
        const auto &surrounding = ic->surroundingText();
        if (!ic->capabilityFlags().test(CapabilityFlag::SurroundingText) || !surrounding.isValid()) {
            return "0\t";
        }
        const std::string &text = surrounding.text();
        const size_t length = utf8::lengthValidated(text);
        const size_t cursor = surrounding.cursor();
        if (length == utf8::INVALID_LENGTH || cursor > length) {
            return "0\t";
        }
        const auto at = [&](size_t chars) { return utf8::nextNChar(text.begin(), chars); };
        std::string escaped;
        for (auto it = at(cursor > 200 ? cursor - 200 : 0), end = at(cursor); it != end; ++it) {
            switch (*it) {
            case '\\': escaped += "\\\\"; break;
            case '\t': escaped += "\\t"; break;
            case '\n': escaped += "\\n"; break;
            default: escaped += *it;
            }
        }
        return "1\t" + escaped;
    }

    // ---- backend messages --------------------------------------------------------------------------

    void onLine(const std::string &line) {
        std::vector<std::string> fields;
        size_t from = 0;
        for (size_t tab; (tab = line.find('\t', from)) != std::string::npos; from = tab + 1) {
            fields.push_back(line.substr(from, tab - from));
        }
        fields.push_back(line.substr(from));
        const std::string &kind = fields[0];

        if (kind == "READY") {
            ready_ = true;
            WHISTLE_INFO() << "backend ready";
        } else if (kind == "ERROR") {
            WHISTLE_INFO() << "backend error: " << (fields.size() > 1 ? fields[1] : "");
            showStatus(findIc(), "⚠ " + (fields.size() > 1 ? fields[1] : std::string("error")));
            errorShown_ = true;
        } else if (state_ == State::Cancelling) {
            if (kind == "DONE") {
                state_ = State::Idle;
            }  // anything else belonged to the cancelled session
        } else if (kind == "COMMIT" && fields.size() == 2) {
            if (auto *ic = findIc()) {
                // Commit while the preedit is still up, then let the next PREEDIT replace it: the order
                // clients handle reliably (a commit right after clearing the preedit can be swallowed).
                ic->commitString(fields[1]);
            }
        } else if (kind == "PREEDIT" && fields.size() == 3) {
            setPreedit(fields[1], fields[2]);
        } else if (kind == "DONE") {
            logContext(findIc(), "DONE");
            endSession(State::Idle);
        } else {
            WHISTLE_INFO() << "unexpected backend line: " << line;
        }
    }

    void setPreedit(const std::string &settled, const std::string &tentative) {
        auto *ic = findIc();
        if (!ic) {
            return;
        }
        Text preedit;
        if (!settled.empty()) {
            preedit.append(settled, TextFormatFlag::Underline);
        }
        if (!tentative.empty()) {
            preedit.append(tentative, {TextFormatFlag::Underline, TextFormatFlag::HighLight});
        }
        preedit.setCursor(preedit.textLength());
        ic->inputPanel().setClientPreedit(preedit);
        ic->updatePreedit();
    }

    // Leave the session: clear the preedit, restore the field's capabilities and replay held keys.
    void endSession(State next) {
        auto *ic = findIc();
        if (ic) {
            ic->inputPanel().setClientPreedit(Text());
            ic->updatePreedit();
            ic->setCapabilityFlags(savedCaps_);
            if (!errorShown_) {  // leave an error on screen; the next session clears it
                ic->inputPanel().setAuxUp(Text());
                ic->updateUserInterface(UserInterfaceComponent::InputPanel);
            }
        }
        const auto held = std::move(held_);
        held_.clear();
        state_ = next;
        if (next == State::Idle && ic) {
            for (const auto &key : held) {
                replay(ic, key);
            }
        }
    }

    // Typed text goes back as a commit, which every frontend delivers; other keys (Backspace, Return,
    // arrows) are forwarded to the app as key events.
    static void replay(InputContext *ic, const Key &key) {
        const uint32_t ch = Key::keySymToUnicode(key.sym());
        const bool printable = ch >= 0x20 && ch != 0x7f &&
                               !key.states().testAny(KeyStates{KeyState::Ctrl, KeyState::Alt, KeyState::Super});
        if (printable) {
            ic->commitString(Key::keySymToUTF8(key.sym()));
        } else {
            ic->forwardKey(key);
        }
    }

    // ---- backend process ---------------------------------------------------------------------------

    void spawnBackend() {
        const std::string &command = config_.backend.value();
        if (command.empty()) {
            WHISTLE_INFO() << "no Backend command configured";
            return;
        }
        int fds[2];
        // A socket rather than pipes: writing to a dead backend fails with EPIPE (MSG_NOSIGNAL) instead of
        // raising SIGPIPE, which would kill fcitx5.
        if (::socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, fds) != 0) {
            WHISTLE_INFO() << "socketpair failed: " << std::strerror(errno);
            return;
        }
        posix_spawn_file_actions_t actions;
        posix_spawn_file_actions_init(&actions);
        posix_spawn_file_actions_adddup2(&actions, fds[1], STDIN_FILENO);
        posix_spawn_file_actions_adddup2(&actions, fds[1], STDOUT_FILENO);
        const char *argv[] = {"/bin/sh", "-c", command.c_str(), nullptr};
        const int rc = ::posix_spawn(&child_, "/bin/sh", &actions, nullptr, const_cast<char **>(argv), environ);
        posix_spawn_file_actions_destroy(&actions);
        ::close(fds[1]);
        if (rc != 0) {
            WHISTLE_INFO() << "cannot start backend: " << std::strerror(rc);
            ::close(fds[0]);
            child_ = -1;
            return;
        }
        backendFd_ = fds[0];
        ::fcntl(backendFd_, F_SETFL, ::fcntl(backendFd_, F_GETFL) | O_NONBLOCK);
        ready_ = false;
        buffer_.clear();
        WHISTLE_INFO() << "started backend pid " << child_ << ": " << command;
        ioSource_ = instance_->eventLoop().addIOEvent(
            backendFd_, IOEventFlag::In, [this](EventSourceIO *, int fd, IOEventFlags flags) {
                return onReadable(fd, flags);
            });
    }

    bool onReadable(int fd, IOEventFlags flags) {
        char chunk[4096];
        bool closed = false;
        while (true) {
            const ssize_t n = ::read(fd, chunk, sizeof(chunk));
            if (n > 0) {
                buffer_.append(chunk, static_cast<size_t>(n));
            } else if (n < 0 && (errno == EAGAIN || errno == EINTR)) {
                break;
            } else {
                closed = true;
                break;
            }
        }
        for (size_t newline; (newline = buffer_.find('\n')) != std::string::npos;) {
            const std::string line = buffer_.substr(0, newline);
            buffer_.erase(0, newline + 1);
            onLine(line);
        }
        if (closed || flags.testAny(IOEventFlags{IOEventFlag::Hup, IOEventFlag::Err})) {
            WHISTLE_INFO() << "backend exited";
            if (state_ != State::Idle) {
                showStatus(findIc(), "⚠ Whistle backend exited");
                errorShown_ = true;
                endSession(State::Idle);
            }
            reapBackend();
            return false;
        }
        return true;
    }

    void send(const std::string &line) {
        const std::string data = line + "\n";
        if (backendFd_ < 0 || ::send(backendFd_, data.data(), data.size(), MSG_NOSIGNAL) !=
                                  static_cast<ssize_t>(data.size())) {
            WHISTLE_INFO() << "cannot send to backend: " << line;
        }
    }

    void reapBackend() {
        ioSource_.reset();
        if (backendFd_ >= 0) {
            ::close(backendFd_);
            backendFd_ = -1;
        }
        if (child_ > 0) {
            ::waitpid(child_, nullptr, WNOHANG);
            child_ = -1;
        }
        ready_ = false;
    }

    void stopBackend() {
        // Closing the socket ends the backend's input, and it exits; SIGTERM covers a stuck one.
        const pid_t child = child_;
        child_ = -1;
        reapBackend();
        if (child > 0) {
            ::kill(child, SIGTERM);
            ::waitpid(child, nullptr, 0);
        }
    }

    // ---- context logging ---------------------------------------------------------------------------

    void logContext(InputContext *ic, const char *when) const {
        if (!config_.logContext.value() || !ic) {
            return;
        }
        static const std::pair<CapabilityFlag, const char *> names[] = {
            {CapabilityFlag::Preedit, "Preedit"},
            {CapabilityFlag::FormattedPreedit, "FormattedPreedit"},
            {CapabilityFlag::ClientUnfocusCommit, "ClientUnfocusCommit"},
            {CapabilityFlag::SurroundingText, "SurroundingText"},
            {CapabilityFlag::Password, "Password"},
            {CapabilityFlag::Sensitive, "Sensitive"},
            {CapabilityFlag::Email, "Email"},
            {CapabilityFlag::Url, "Url"},
            {CapabilityFlag::Terminal, "Terminal"},
            {CapabilityFlag::Multiline, "Multiline"},
            {CapabilityFlag::Name, "Name"},
            {CapabilityFlag::Number, "Number"},
            {CapabilityFlag::Digit, "Digit"},
            {CapabilityFlag::Dialable, "Dialable"},
            {CapabilityFlag::Alpha, "Alpha"},
            {CapabilityFlag::Date, "Date"},
            {CapabilityFlag::Time, "Time"},
            {CapabilityFlag::Lowercase, "Lowercase"},
            {CapabilityFlag::Uppercase, "Uppercase"},
            {CapabilityFlag::UppercaseWords, "UppercaseWords"},
            {CapabilityFlag::UppwercaseSentences, "UppercaseSentences"},
            {CapabilityFlag::NoAutoUpperCase, "NoAutoUpperCase"},
            {CapabilityFlag::SpellCheck, "SpellCheck"},
            {CapabilityFlag::NoSpellCheck, "NoSpellCheck"},
            {CapabilityFlag::WordCompletion, "WordCompletion"},
            {CapabilityFlag::CommitStringWithCursor, "CommitStringWithCursor"},
        };
        std::string caps;
        for (const auto &[flag, name] : names) {
            if (ic->capabilityFlags().test(flag)) {
                caps += caps.empty() ? name : std::string(",") + name;
            }
        }
        std::string around = "(none)";
        const auto &surrounding = ic->surroundingText();
        if (surrounding.isValid()) {
            const std::string &text = surrounding.text();
            const size_t length = utf8::lengthValidated(text);
            const size_t cursor = surrounding.cursor();
            if (length == utf8::INVALID_LENGTH || cursor > length) {
                around = "(invalid: cursor " + std::to_string(cursor) + " of " + std::to_string(length) + ")";
            } else {
                // Up to 60 characters either side of the cursor, which is marked by ⸢|⸥.
                const size_t from = cursor > 60 ? cursor - 60 : 0;
                const size_t to = std::min(length, cursor + 60);
                auto at = [&](size_t chars) { return utf8::nextNChar(text.begin(), chars); };
                std::string excerpt = std::string(at(from), at(cursor)) + "⸢|⸥" + std::string(at(cursor), at(to));
                std::string escaped;
                for (char c : excerpt) {
                    escaped += c == '\n' ? std::string("\\n") : c == '\t' ? std::string("\\t") : std::string(1, c);
                }
                around = "cursor " + std::to_string(cursor) + "/" + std::to_string(length) + " anchor " +
                         std::to_string(surrounding.anchor()) + " \"" + escaped + "\"";
            }
        }
        WHISTLE_INFO() << "context " << when << ": program=" << ic->program() << " frontend=" << ic->frontend()
                       << " caps=" << (caps.empty() ? "-" : caps) << " surrounding=" << around;
    }

    // ---- helpers -----------------------------------------------------------------------------------

    InputContext *findIc() const { return instance_->inputContextManager().findByUUID(icUuid_); }

    static void showStatus(InputContext *ic, const std::string &message) {
        if (!ic) {
            return;
        }
        ic->inputPanel().setAuxUp(Text(message));
        ic->updateUserInterface(UserInterfaceComponent::InputPanel);
    }

    Instance *instance_;
    WhistleImeConfig config_;
    std::unique_ptr<HandlerTableEntry<EventHandler>> keyHandler_;
    std::unique_ptr<HandlerTableEntry<EventHandler>> focusHandler_;
    std::unique_ptr<EventSourceIO> ioSource_;
    pid_t child_ = -1;
    int backendFd_ = -1;
    bool ready_ = false;
    std::string buffer_;
    State state_ = State::Idle;
    ICUUID icUuid_{};
    CapabilityFlags savedCaps_{};
    std::vector<Key> held_;  // keys pressed after STOP, replayed in order after the final commit
    bool errorShown_ = false;
};

}  // namespace fcitx

class WhistleImeFactory : public fcitx::AddonFactory {
    fcitx::AddonInstance *create(fcitx::AddonManager *manager) override {
        return new fcitx::WhistleIme(manager->instance());
    }
};

FCITX_ADDON_FACTORY(WhistleImeFactory);
