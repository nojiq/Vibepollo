#include <gtest/gtest.h>
#include "src/platform/macos/layout_change_observer.h"

namespace {
  using observer_t = platf::macos_virtual_display::layout_change_observer_t;
  using namespace std::chrono_literals;
  const auto start = observer_t::clock::time_point {};
  const observer_t::positions_t initial {{"surface", {1920, 0, 2160, 1440}}, {"msi", {4080, 0, 1920, 1080}}};
}

TEST(MacLayoutMemory, SavesOnlySettledManualChanges) {
  observer_t observer;
  EXPECT_FALSE(observer.observe(initial, start));
  auto moved = initial;
  moved["msi"] = {-1920, 500, 1920, 1080};
  EXPECT_FALSE(observer.observe(moved, start + 100ms));
  EXPECT_FALSE(observer.observe(moved, start + 400ms));
  EXPECT_TRUE(observer.observe(moved, start + 600ms));
  EXPECT_FALSE(observer.observe(moved, start + 1s));
}

TEST(MacLayoutMemory, ReconnectAndRemovalCannotOverwriteManualArrangement) {
  observer_t observer;
  observer.observe(initial, start);
  auto adjusted = initial;
  adjusted.erase("msi");
  adjusted["surface"] = {0, 0, 2160, 1440};
  observer.topology_changed(start + 1s);
  EXPECT_FALSE(observer.observe(adjusted, start + 2s));
  EXPECT_FALSE(observer.observe(adjusted, start + 3s));
  EXPECT_FALSE(observer.observe(adjusted, start + 4s));
  adjusted["surface"] = {1920, 300, 2160, 1440};
  EXPECT_FALSE(observer.observe(adjusted, start + 5s));
  EXPECT_TRUE(observer.observe(adjusted, start + 6s));
}

TEST(MacLayoutMemory, CancelsDragThatReturnsToSavedPosition) {
  observer_t observer;
  observer.observe(initial, start);
  auto moved = initial;
  moved["surface"] = {1920, 500, 2160, 1440};
  EXPECT_FALSE(observer.observe(moved, start + 1s));
  EXPECT_FALSE(observer.observe(initial, start + 2s));
  EXPECT_FALSE(observer.observe(moved, start + 3s));
  EXPECT_TRUE(observer.observe(moved, start + 4s));
}

TEST(MacLayoutMemory, KeepsUserMoveMadeDuringReconnectSettling) {
  observer_t observer;
  observer.topology_changed(start, initial);
  auto moved = initial;
  moved["msi"] = {-1920, 500, 1920, 1080};
  EXPECT_FALSE(observer.observe(moved, start + 1s));
  EXPECT_FALSE(observer.observe(moved, start + 2s));
  EXPECT_TRUE(observer.observe(moved, start + 3s));
}

TEST(MacLayoutMemory, KeepsMoveOfRemainingDisplayAfterRemoval) {
  observer_t observer;
  observer.topology_changed(start, initial);
  observer.display_removed("msi", start + 1s);
  auto moved = initial;
  moved.erase("msi");
  moved["surface"] = {1920, 300, 2160, 1440};
  EXPECT_FALSE(observer.observe(moved, start + 2s));
  EXPECT_FALSE(observer.observe(moved, start + 3s));
  EXPECT_TRUE(observer.observe(moved, start + 4s));
}

TEST(MacLayoutMemory, ReseedsRawPositionsAfterRemovalBeforeSavingMoves) {
  observer_t observer;
  observer.topology_changed(start, initial);
  observer.display_removed("msi", start + 1s);

  auto shifted = initial;
  shifted.erase("msi");
  shifted["surface"] = {0, 0, 2160, 1440};
  observer.topology_changed(start + 1s);
  EXPECT_FALSE(observer.observe(shifted, start + 3s));

  shifted["surface"] = {100, 200, 2160, 1440};
  EXPECT_FALSE(observer.observe(shifted, start + 4s));
  EXPECT_TRUE(observer.observe(shifted, start + 5s));
}

TEST(MacLayoutMemory, ReportsWhetherAcceptedMoveIncludedPhysicalOutput) {
  observer_t observer;
  const observer_t::positions_t initial {
    {"physical", {0, 0, 1920, 1080}},
    {"remote", {1920, 0, 2160, 1440}},
  };
  observer.topology_changed(start, initial);

  auto remote_move = initial;
  remote_move["remote"] = {100, 200, 2160, 1440};
  EXPECT_FALSE(observer.observe(remote_move, start + 3s, {"physical"}));
  EXPECT_TRUE(observer.observe(remote_move, start + 4s, {"physical"}));
  EXPECT_FALSE(observer.last_change_included_physical());

  auto physical_move = remote_move;
  physical_move["physical"] = {-1920, 40, 1920, 1080};
  EXPECT_FALSE(observer.observe(physical_move, start + 5s, {"physical"}));
  EXPECT_TRUE(observer.observe(physical_move, start + 6s, {"physical"}));
  EXPECT_TRUE(observer.last_change_included_physical());
}
