cmake_minimum_required(VERSION 3.16)
project(whistle-ime LANGUAGES CXX)
include(GNUInstallDirs)

find_package(PkgConfig REQUIRED)
pkg_check_modules(FCITX5CORE REQUIRED IMPORTED_TARGET Fcitx5Core)

set(CMAKE_CXX_STANDARD 20)  # fcitx5 >= 5.1.13 headers use std::span
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(whistle-ime MODULE whistle-ime.cpp)
set_target_properties(whistle-ime PROPERTIES PREFIX "")
target_link_libraries(whistle-ime PRIVATE PkgConfig::FCITX5CORE)
target_compile_options(whistle-ime PRIVATE -Wall -Wextra)

install(TARGETS whistle-ime DESTINATION "${CMAKE_INSTALL_LIBDIR}/fcitx5")
install(FILES whistle-ime.conf DESTINATION "${CMAKE_INSTALL_DATADIR}/fcitx5/addon")
