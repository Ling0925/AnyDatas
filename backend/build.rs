use std::{env, path::PathBuf};

/// 验证 Cargo 外层包装器已经准备好正确的静态库和头文件。
///
/// `libduckdb-sys` 比当前包更早执行 build.rs，因此下载必须发生在 Cargo 启动前；
/// 这里再次检查契约的好处是禁止意外回退到系统动态 DuckDB。
fn verify_prebuilt_contract(target_os: &str) {
    let static_mode = env::var("DUCKDB_STATIC").unwrap_or_default();
    assert_eq!(
        static_mode, "1",
        "必须通过 scripts/with-duckdb-prebuilt.py 构建 backend（DUCKDB_STATIC=1）"
    );
    let no_pkg_config = env::var("DUCKDB_NO_PKG_CONFIG").unwrap_or_default();
    assert_eq!(
        no_pkg_config, "1",
        "必须禁用 DuckDB pkg-config 探测，链接参数只能来自固定平台白名单"
    );

    let lib_dir =
        PathBuf::from(env::var_os("DUCKDB_LIB_DIR").expect("必须通过包装器设置 DUCKDB_LIB_DIR"));
    let include_dir = PathBuf::from(
        env::var_os("DUCKDB_INCLUDE_DIR").expect("必须通过包装器设置 DUCKDB_INCLUDE_DIR"),
    );
    let library = if target_os == "windows" {
        "duckdb_static.lib"
    } else {
        "libduckdb_static.a"
    };
    assert!(
        lib_dir.join(library).is_file(),
        "预编译 DuckDB 静态库不存在：{}",
        lib_dir.join(library).display()
    );
    assert!(
        include_dir.join("duckdb.h").is_file(),
        "预编译 DuckDB 头文件不存在：{}",
        include_dir.join("duckdb.h").display()
    );
}

/// 补充 DuckDB 静态归档不会自动传播到最终 Rust 二进制的系统库。
///
/// 这些库与 duckdb-prebuilt 的 metadata 白名单保持一致，显式输出可让 Windows
/// 和缺少 pkg-config 的 Unix 环境使用完全相同的链接路径。
fn link_system_libraries(target_os: &str) {
    let libraries: &[&str] = match target_os {
        "linux" => &["stdc++", "m", "dl", "pthread"],
        "macos" => &["c++"],
        "windows" => &["ws2_32", "rstrtmgr", "bcrypt"],
        other => panic!("不支持使用预编译 DuckDB 的目标系统：{other}"),
    };
    for library in libraries {
        println!("cargo:rustc-link-lib=dylib={library}");
    }
}

/// 在最终 anydatas-api 链接阶段验证并补齐预编译 DuckDB 的平台依赖。
fn main() {
    println!("cargo:rerun-if-env-changed=DUCKDB_LIB_DIR");
    println!("cargo:rerun-if-env-changed=DUCKDB_INCLUDE_DIR");
    println!("cargo:rerun-if-env-changed=DUCKDB_NO_PKG_CONFIG");
    println!("cargo:rerun-if-env-changed=DUCKDB_STATIC");
    let target_os = env::var("CARGO_CFG_TARGET_OS").expect("Cargo 必须提供 CARGO_CFG_TARGET_OS");
    verify_prebuilt_contract(&target_os);
    link_system_libraries(&target_os);
}
