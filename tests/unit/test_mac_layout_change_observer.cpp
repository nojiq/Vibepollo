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

TEST(MacLayoutMemory, RetainsPhysicalAnchorOriginWhenRemoteDisplayIsRemoved) {
  observer_t observer;
  observer.topology_changed(start, initial);
  observer.display_removed("msi", start + 1s);

  const auto origin = observer.baseline_origin("surface");
  ASSERT_TRUE(origin.has_value());
  EXPECT_EQ(origin->first, 1920);
  EXPECT_EQ(origin->second, 0);
  EXPECT_FALSE(observer.baseline_origin("msi").has_value());
}
