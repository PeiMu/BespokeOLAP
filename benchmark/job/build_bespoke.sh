#!/bin/bash
# Build the Bespoke JOB engine from synthesized code in output/.
# Usage: bash benchmark/job/build_bespoke.sh
set -e

PROJ_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
JOB_SRC="$PROJ_ROOT/output"
API_DIR="$PROJ_ROOT/misc/fasttest"
BUILD_DIR="$PROJ_ROOT/benchmark/job/build"

mkdir -p "$BUILD_DIR/obj"

CXX="${CXX:-g++}"
PKG_CFLAGS=$(pkg-config --cflags arrow parquet)
PKG_LIBS=$(pkg-config --libs arrow parquet)

CXXFLAGS="-g -std=c++20 -fPIC -O3 -flto -I$JOB_SRC -I$API_DIR"
LDFLAGS_SO="-shared -Wl,--build-id=sha1"

echo "Building Bespoke JOB engine..."
echo "  CXX: $CXX"
echo "  Source: $JOB_SRC"
echo "  Build: $BUILD_DIR"

compile_obj() {
    local src="$1"
    local obj="$BUILD_DIR/obj/$(basename "${src%.cpp}.o")"
    echo "  CC $src"
    $CXX $CXXFLAGS $PKG_CFLAGS -c "$src" -o "$obj"
}

compile_obj "$API_DIR/loader_api.cpp"
compile_obj "$JOB_SRC/loader_impl.cpp"
compile_obj "$API_DIR/loader_utils.cpp"
compile_obj "$API_DIR/builder_api.cpp"
compile_obj "$JOB_SRC/builder_impl.cpp"
compile_obj "$API_DIR/query_api.cpp"
compile_obj "$JOB_SRC/query_impl.cpp"

QUERY_OBJS=""
for qsrc in "$JOB_SRC"/query_q*.cpp; do
    [ -f "$qsrc" ] || continue
    compile_obj "$qsrc"
    QUERY_OBJS="$QUERY_OBJS $BUILD_DIR/obj/$(basename "${qsrc%.cpp}.o")"
done

compile_obj "$API_DIR/db.cpp"
compile_obj "$API_DIR/utils/build_id.cpp"

echo "  LINK libloader.so"
$CXX $LDFLAGS_SO -o "$BUILD_DIR/libloader.so" \
    "$BUILD_DIR/obj/loader_api.o" "$BUILD_DIR/obj/loader_impl.o" "$BUILD_DIR/obj/loader_utils.o" \
    $PKG_LIBS

echo "  LINK libbuilder.so"
$CXX $LDFLAGS_SO -o "$BUILD_DIR/libbuilder.so" \
    "$BUILD_DIR/obj/builder_api.o" "$BUILD_DIR/obj/builder_impl.o" \
    $PKG_LIBS

echo "  LINK libquery.so"
$CXX $LDFLAGS_SO -o "$BUILD_DIR/libquery.so" \
    "$BUILD_DIR/obj/query_api.o" "$BUILD_DIR/obj/query_impl.o" $QUERY_OBJS \
    $PKG_LIBS

echo "  LINK db"
$CXX -o "$BUILD_DIR/db" \
    "$BUILD_DIR/obj/db.o" "$BUILD_DIR/obj/build_id.o" \
    -ldl -lpthread $PKG_LIBS

echo "Done. Binary: $BUILD_DIR/db"
