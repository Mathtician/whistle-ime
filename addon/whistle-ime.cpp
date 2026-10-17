/*
 * whistle-ime: dictation into the focused text field, with a live preview.
 *
 * The addon is thin. It keeps one backend process running (the speech model stays loaded), turns the
 * trigger key (or the D-Bus method Toggle, which `whistle-ime toggle` calls) into START/STOP, shows the
 * backend's PREEDIT as inline preedit and inserts its COMMITs.
 * The backend owns every decision about text, including spacing; see src/whistle_ime/backend.py for the
 * protocol. Structure and several hard-won details follow fcitx5-voiceinput (MIT,
 * github.com/GavinLiuOnline/fcitx5-voiceinput).
 *
 * Session states:
 *   Idle -> Recording        trigger key or Toggle, into the focused text field
 *   Recording -> Finishing   trigger key, Toggle or Escape (STOP); any other key also STOPs, carrying the
 *                            key, and the backend decides from it what to commit (finish.py); a key it
 *                            did not absorb is replayed after the final commit, so it lands after the
 *                            dictated text
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

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <set>
#include <sstream>
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
#include <fcitx-module/dbus/dbus_public.h>
#include <fcitx-utils/dbus/bus.h>
#include <fcitx-utils/dbus/objectvtable.h>

#include "shadow.h"

extern char **environ;

namespace fcitx {

FCITX_DEFINE_LOG_CATEGORY(whistle_ime, "whistle-ime");
#define WHISTLE_INFO() FCITX_LOGC(::fcitx::whistle_ime, Info)

FCITX_CONFIGURATION(
    WhistleImeConfig,
    // KWin checks its global shortcuts before an input method sees a key, so a chord KWin also uses never
    // arrives here; the addon warns at startup if kglobalshortcutsrc has it.
    KeyListOption triggerKey{this, "TriggerKey", "Trigger Key", {Key("Super+Alt+V")}, KeyListConstrain()};
    // Run with /bin/sh -c; it must speak the protocol on stdin/stdout.
    Option<std::string> backend{this, "Backend", "Backend Command", ""};
    // Logs what the app reports about the field (including the text around the cursor) at START and DONE.
    Option<bool> logContext{this, "LogContext", "Log Field Context", false};);

class WhistleIme;

// org.fcitx.Fcitx.WhistleIme1 at /whistle_ime on fcitx5's bus: what `whistle-ime toggle` calls, for
// scripts or a desktop shortcut as an alternative to the trigger key.
class WhistleImeDBus : public dbus::ObjectVTable<WhistleImeDBus> {
public:
    explicit WhistleImeDBus(WhistleIme *parent) : parent_(parent) {}
    std::string toggle();

private:
    WhistleIme *parent_;
    FCITX_OBJECT_VTABLE_METHOD(toggle, "Toggle", "", "s");
};

class WhistleIme final : public AddonInstance {
    // Defines dbus(); it must come before its first use, since its return type is deduced.
    FCITX_ADDON_DEPENDENCY_LOADER(dbus, instance_->addonManager());

public:
    explicit WhistleIme(Instance *instance) : instance_(instance) {
        readAsIni(config_, "conf/whistle-ime.conf");
        warnAboutKWinCollisions();
        spawnBackend();
        if (auto *bus = dbus() ? dbus()->call<IDBusModule::bus>() : nullptr) {
            bus->addObjectVTable("/whistle_ime", "org.fcitx.Fcitx.WhistleIme1", dbusObject_);
        } else {
            WHISTLE_INFO() << "no D-Bus: whistle-ime toggle will not reach this addon";
        }

        keyHandler_ = instance_->watchEvent(
            EventType::InputContextKeyEvent, EventWatcherPhase::PreInputMethod,
            [this](Event &event) { onKey(static_cast<KeyEvent &>(event)); });

        cursorHandler_ = instance_->watchEvent(
            EventType::InputContextCursorRectChanged, EventWatcherPhase::Default, [this](Event &event) {
                auto *ic = static_cast<InputContextEvent &>(event).inputContext();
                if (shadow_.tracks(ic->uuid())) {
                    shadow_.cursorMoved(ic->cursorRect(), state_ != State::Idle, Shadow::Clock::now());
                }
            });

        focusHandler_ = instance_->watchEvent(
            EventType::InputContextFocusOut, EventWatcherPhase::Default, [this](Event &event) {
                auto &focus = static_cast<FocusOutEvent &>(event);
                if (shadow_.tracks(focus.inputContext()->uuid())) {
                    shadow_.invalidate();
                }
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

    // Start dictating into the focused text field, or stop. Returns what happened, for the caller to print.
    std::string toggle() {
        switch (state_) {
        case State::Idle: {
            auto *ic = instance_->inputContextManager().lastFocusedInputContext();
            if (!ic || !ic->hasFocus()) {
                return "no text field has focus";
            }
            return start(ic);
        }
        case State::Recording:
            stop(nullptr);
            return "stopping";
        case State::Finishing:
            return "already stopping";
        case State::Cancelling:
            return "busy cancelling; try again";
        }
        return "";
    }

private:
    enum class State { Idle, Recording, Finishing, Cancelling };

    // ---- keys -------------------------------------------------------------------------------------

    void onKey(KeyEvent &event) {
        const Key &key = event.key();
        const bool trigger = key.checkKeyList(config_.triggerKey.value());
        if (event.isRelease()) {
            if (trigger) {
                event.filterAndAccept();  // its press was ours, so its release is too
            }
            return;
        }
        switch (state_) {
        case State::Idle:
            if (trigger) {
                start(event.inputContext());
                event.filterAndAccept();
            } else {
                follow(event.inputContext(), key);  // the key goes on to the app
            }
            return;
        case State::Recording:
            if (trigger || key.check(FcitxKey_Escape)) {
                stop(nullptr);
                event.filterAndAccept();
            } else if (!key.isModifier()) {
                // The key ends the dictation; the backend decides from it what to commit, and unless it
                // absorbs the key, it is replayed after the final commit.
                held_.push_back(key);
                stop(&key);
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

    // Warn if KWin has a global shortcut on our trigger: KWin would take the key before fcitx5 sees it.
    void warnAboutKWinCollisions() const {
        const char *home = std::getenv("HOME");
        const char *xdg = std::getenv("XDG_CONFIG_HOME");
        const std::string path = (xdg && *xdg ? std::string(xdg) : std::string(home ? home : "") + "/.config") +
                                 "/kglobalshortcutsrc";
        std::ifstream file(path);
        std::string line;
        std::string group;
        while (std::getline(file, line)) {
            if (!line.empty() && line.front() == '[') {
                group = line;
                continue;
            }
            const auto eq = line.find('=');
            // _k_friendly_name is a label; custom command shortcuts are stored as _launch=, so keep those
            if (eq == std::string::npos || line.compare(0, eq, "_k_friendly_name") == 0) {
                continue;
            }
            // name=active shortcuts (tab-separated),default,description
            const std::string active = line.substr(eq + 1, line.find(',', eq) - eq - 1);
            std::stringstream shortcuts(active);
            for (std::string shortcut; std::getline(shortcuts, shortcut, '\t');) {
                for (const auto &trigger : config_.triggerKey.value()) {
                    if (sameChord(shortcut, trigger)) {
                        WHISTLE_INFO() << "KWin's shortcut " << group << " " << line.substr(0, eq) << " uses "
                                       << shortcut << ", the dictation trigger; KWin will take that key first";
                    }
                }
            }
        }
    }

    // A KDE shortcut ("Meta+Alt+V") and an fcitx5 key name the same chord.
    static bool sameChord(const std::string &kde, const Key &key) {
        std::set<std::string> mods;
        std::string name;
        std::stringstream parts(kde);
        for (std::string part; std::getline(parts, part, '+');) {
            std::string lower(part);
            std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return std::tolower(c); });
            if (lower == "meta" || lower == "super") {
                mods.insert("super");
            } else if (lower == "ctrl" || lower == "control") {
                mods.insert("ctrl");
            } else if (lower == "alt" || lower == "shift") {
                mods.insert(lower);
            } else {
                name = lower;
            }
        }
        std::set<std::string> want;
        const auto states = key.states();
        if (states.test(KeyState::Super)) want.insert("super");
        if (states.test(KeyState::Ctrl)) want.insert("ctrl");
        if (states.test(KeyState::Alt)) want.insert("alt");
        if (states.test(KeyState::Shift)) want.insert("shift");
        std::string keyName = Key::keySymToString(key.sym());
        std::transform(keyName.begin(), keyName.end(), keyName.begin(), [](unsigned char c) { return std::tolower(c); });
        return !name.empty() && name == keyName && mods == want;
    }

    std::string start(InputContext *ic) {
        if (backendFd_ < 0) {
            spawnBackend();  // it died earlier; this press brings it back but cannot use it yet
        }
        if (!ready_) {
            showStatus(ic, "Whistle is loading…");
            return "still loading";
        }
        // Read the context before touching the shadow: an unusable shadow is restarted below.
        const std::string before = context(ic);
        if (!shadow_.usable(ic->uuid(), ic->cursorRect())) {
            shadow_.reset(ic->uuid(), ic->cursorRect(), Shadow::Clock::now());
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
        send("START\t" + before);
        showStatus(ic, "🎤 Dictating");
        return "started";
    }

    // STOP, with the key that ended the dictation if there was one: keysym name, modifiers, escaped text.
    void stop(const Key *key) {
        state_ = State::Finishing;
        if (!key) {
            send("STOP");
        } else {
            std::string mods;
            const auto add = [&](KeyState state, const char *name) {
                if (key->states().test(state)) {
                    mods += mods.empty() ? name : std::string(",") + name;
                }
            };
            add(KeyState::Shift, "shift");
            add(KeyState::Ctrl, "ctrl");
            add(KeyState::Alt, "alt");
            add(KeyState::Super, "super");
            send("STOP\t" + Key::keySymToString(key->sym()) + "\t" + mods + "\t" + escape(typedText(*key)));
        }
        showStatus(findIc(), "Finishing…");
    }

    // START's context fields: "1<TAB><text before the cursor>" if known, else "0<TAB>". The backend decides
    // spacing and casing from it. In order: the app's report if it has text; else the shadow, if it still
    // describes this field; else the app's empty report (the start of a field); else unknown.
    std::string context(InputContext *ic) const {
        const auto &surrounding = ic->surroundingText();
        const bool reported = ic->capabilityFlags().test(CapabilityFlag::SurroundingText) && surrounding.isValid();
        if (reported && !surrounding.text().empty()) {
            const std::string &text = surrounding.text();
            const size_t length = utf8::lengthValidated(text);
            const size_t cursor = surrounding.cursor();
            if (length != utf8::INVALID_LENGTH && cursor <= length) {
                return "1\t" + escape(std::string(utf8::nextNChar(text.begin(), cursor > 200 ? cursor - 200 : 0),
                                                  utf8::nextNChar(text.begin(), cursor)));
            }
            return "0\t";
        }
        if (shadow_.usable(ic->uuid(), ic->cursorRect()) && !shadow_.text().empty()) {
            return "1\t" + escape(shadow_.text());
        }
        return reported ? "1\t" : "0\t";
    }

    // Backslash, tab and newline escaped, so the context fits in one field of one line.
    static std::string escape(const std::string &text) {
        std::string escaped;
        for (char c : text) {
            switch (c) {
            case '\\': escaped += "\\\\"; break;
            case '\t': escaped += "\\t"; break;
            case '\n': escaped += "\\n"; break;
            default: escaped += c;
            }
        }
        return escaped;
    }

    // A key reaching the app while we are idle: printable text extends the shadow, Backspace shortens it,
    // modifiers alone do nothing, and anything else (Enter, arrows, Home, Tab, Escape, chords) may move the
    // cursor or end the line, so the shadow no longer knows where it is.
    void follow(InputContext *ic, const Key &key) {
        if (!ic || !shadow_.tracks(ic->uuid()) || key.isModifier()) {
            return;
        }
        if (const auto text = typedText(key); !text.empty()) {
            shadow_.append(text, Shadow::Clock::now());
        } else if (key.check(FcitxKey_BackSpace)) {
            shadow_.backspace(Shadow::Clock::now());
        } else {
            shadow_.invalidate();
        }
    }

    // The text a key types, or "" if it types none (Ctrl/Alt/Super chords type none).
    static std::string typedText(const Key &key) {
        const uint32_t ch = Key::keySymToUnicode(key.sym());
        if (ch < 0x20 || ch == 0x7f ||
            key.states().testAny(KeyStates{KeyState::Ctrl, KeyState::Alt, KeyState::Super})) {
            return "";
        }
        return Key::keySymToUTF8(key.sym());
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
                if (shadow_.tracks(icUuid_)) {
                    shadow_.append(fields[1], Shadow::Clock::now());
                }
            }
        } else if (kind == "PREEDIT" && fields.size() == 3) {
            setPreedit(fields[1], fields[2]);
            shadow_.touch(Shadow::Clock::now());
        } else if (kind == "DONE") {
            logContext(findIc(), "DONE");
            endSession(State::Idle, fields.size() > 1 && fields[1] == "1");
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
    // Leave the session: clear the preedit, restore the field's capabilities and replay held keys (all but
    // the one that ended the dictation, if the backend absorbed it).
    void endSession(State next, bool absorbed = false) {
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
        auto held = std::move(held_);
        held_.clear();
        if (absorbed && !held.empty()) {
            held.erase(held.begin());
        }
        state_ = next;
        if (next == State::Idle && ic) {
            shadow_.touch(Shadow::Clock::now());  // the app is about to redraw after the final commit
            for (const auto &key : held) {
                replay(ic, key);
                follow(ic, key);
            }
        }
    }

    // Typed text goes back as a commit, which every frontend delivers; other keys (Backspace, Return,
    // arrows) are forwarded to the app as key events.
    static void replay(InputContext *ic, const Key &key) {
        if (const auto text = typedText(key); !text.empty()) {
            ic->commitString(text);
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
    std::unique_ptr<HandlerTableEntry<EventHandler>> cursorHandler_;
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
    Shadow shadow_;
    WhistleImeDBus dbusObject_{this};
};

std::string WhistleImeDBus::toggle() { return parent_->toggle(); }

}  // namespace fcitx

class WhistleImeFactory : public fcitx::AddonFactory {
    fcitx::AddonInstance *create(fcitx::AddonManager *manager) override {
        return new fcitx::WhistleIme(manager->instance());
    }
};

FCITX_ADDON_FACTORY(WhistleImeFactory);
