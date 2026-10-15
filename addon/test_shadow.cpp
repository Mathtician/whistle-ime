// Tests for Shadow (shadow.h); run by ctest, and so by the Nix build.
#include <cassert>
#include <cstdio>
#include <string>

#include "shadow.h"

using fcitx::ICUUID;
using fcitx::Rect;
using fcitx::Shadow;
using namespace std::chrono_literals;

static int failures = 0;
#define CHECK(cond)                                                                \
    do {                                                                           \
        if (!(cond)) {                                                             \
            std::fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #cond); \
            ++failures;                                                            \
        }                                                                          \
    } while (0)

int main() {
    const ICUUID konsole{1}, firefox{2};
    const Rect here(10, 20, 11, 40), there(50, 20, 51, 40);
    const Shadow::Clock::time_point t0{};

    {  // committed text then typing, backspacing past what it knows
        Shadow s;
        CHECK(!s.tracks(konsole));
        s.reset(konsole, here, t0);
        CHECK(s.tracks(konsole) && !s.tracks(firefox) && s.text().empty());
        s.append("When I start a sentence like this.", t0);
        s.backspace(t0);
        CHECK(s.text() == "When I start a sentence like this");
        s.append(" é", t0);
        s.backspace(t0);
        CHECK(s.text() == "When I start a sentence like this ");  // a whole character, not a byte
        Shadow empty;
        empty.reset(konsole, here, t0);
        empty.backspace(t0);
        CHECK(!empty.tracks(konsole));  // deleting text it never saw
    }
    {  // only the tail is kept
        Shadow s;
        s.reset(konsole, here, t0);
        s.append(std::string(Shadow::keep + 50, 'a') + "end", t0);
        CHECK(fcitx::utf8::length(s.text()) == Shadow::keep && s.text().substr(s.text().size() - 3) == "end");
    }
    {  // the cursor: followed right after our change, fatal otherwise
        Shadow s;
        s.reset(konsole, here, t0);
        s.append("text", t0);
        s.cursorMoved(there, false, t0 + 500ms);  // the app redrawing after our commit
        CHECK(s.usable(konsole, there) && !s.usable(konsole, here) && !s.usable(firefox, there));
        s.cursorMoved(here, false, t0 + 3s);  // nobody here moved it: a click
        CHECK(!s.tracks(konsole));
    }
    {  // during a dictation every move is ours
        Shadow s;
        s.reset(konsole, here, t0);
        s.cursorMoved(there, true, t0 + 10s);
        CHECK(s.usable(konsole, there));
    }
    {  // invalidation is final until the next reset
        Shadow s;
        s.reset(konsole, here, t0);
        s.invalidate();
        CHECK(!s.usable(konsole, here));
        s.reset(konsole, here, t0);
        CHECK(s.usable(konsole, here));
    }
    if (failures == 0) {
        std::puts("shadow: all checks passed");
    }
    return failures == 0 ? 0 : 1;
}
