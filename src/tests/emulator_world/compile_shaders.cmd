@echo off
rem The shaders of emulator_world, compiled with the fxc of the Windows SDK into the headers target.cpp includes.
setlocal
set FXC=fxc
where fxc >nul 2>nul || for /f "delims=" %%f in ('dir /b /s "%ProgramFiles(x86)%\Windows Kits\10\bin\*fxc.exe" ^| findstr /i x64') do set FXC=%%f
cd /d "%~dp0"
"%FXC%" /nologo /T vs_4_0 /E vs /Vn SceneVertexShader /Fh scene_vs.h scene.hlsl || exit /b 1
"%FXC%" /nologo /T ps_4_0 /E ps /Vn ScenePixelShader /Fh scene_ps.h scene.hlsl || exit /b 1
"%FXC%" /nologo /T vs_5_1 /E vs /Vn Scene12VertexShader /Fh scene12_vs.h scene.hlsl || exit /b 1
"%FXC%" /nologo /T ps_5_1 /E ps /Vn Scene12PixelShader /Fh scene12_ps.h scene.hlsl || exit /b 1
"%FXC%" /nologo /T vs_2_0 /E vs9 /Vn Scene9VertexShader /Fh stages_vs9.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T ps_2_0 /E ps9 /Vn Scene9PixelShader /Fh stages_ps9.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T vs_5_0 /E vs_point /Vn PointVertexShader /Fh stages_vs_point.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T gs_5_0 /E gs_square /Vn SquareGeometryShader /Fh stages_gs.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T ps_5_0 /E ps_color /Vn ColorPixelShader /Fh stages_ps_color.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T vs_5_0 /E vs_patch /Vn PatchVertexShader /Fh stages_vs_patch.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T hs_5_0 /E hs_patch /Vn PatchHullShader /Fh stages_hs.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T ds_5_0 /E ds_patch /Vn PatchDomainShader /Fh stages_ds.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T cs_5_0 /E cs_fill /Vn FillComputeShader /Fh stages_cs.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T cs_5_0 /E cs_load /Vn MoreLoadComputeShader /Fh more_cs_load.h more.hlsl || exit /b 1
"%FXC%" /nologo /T vs_5_0 /E vs_screen /Vn MoreScreenVertexShader /Fh more_vs_screen.h more.hlsl || exit /b 1
"%FXC%" /nologo /T ps_5_0 /E ps_deriv /Vn MoreDerivPixelShader /Fh more_ps_deriv.h more.hlsl || exit /b 1
"%FXC%" /nologo /T vs_5_0 /E vs_patch_point /Vn MorePatchVertexShader /Fh more_vs_patch_point.h more.hlsl || exit /b 1
"%FXC%" /nologo /T hs_5_0 /E hs_integer /Vn MoreIntegerHullShader /Fh more_hs_integer.h more.hlsl || exit /b 1
"%FXC%" /nologo /T hs_5_0 /E hs_odd /Vn MoreOddHullShader /Fh more_hs_odd.h more.hlsl || exit /b 1
"%FXC%" /nologo /T ds_5_0 /E ds_edges /Vn MoreEdgesDomainShader /Fh more_ds_edges.h more.hlsl || exit /b 1
"%FXC%" /nologo /T vs_5_0 /E vs_seed /Vn MoreSeedVertexShader /Fh more_vs_seed.h more.hlsl || exit /b 1
"%FXC%" /nologo /T gs_5_0 /E gs_triangle /Vn MoreTriangleGeometryShader /Fh more_gs_triangle.h more.hlsl || exit /b 1
"%FXC%" /nologo /T vs_5_0 /E vs_streamed /Vn MoreStreamedVertexShader /Fh more_vs_streamed.h more.hlsl || exit /b 1
"%FXC%" /nologo /T vs_5_0 /E vs_plain /Vn MorePlainVertexShader /Fh more_vs_plain.h more.hlsl || exit /b 1
"%FXC%" /nologo /T ps_5_0 /E ps_paint /Vn MorePaintPixelShader /Fh more_ps_paint.h more.hlsl || exit /b 1
"%FXC%" /nologo /T fx_4_0 /Vn MoreEffect /Fh more_fx10.h more_fx10.fx || exit /b 1
set DXC=dxc
where dxc >nul 2>nul || for /f "delims=" %%f in ('dir /b /s "%ProgramFiles(x86)%\Windows Kits\10\bin\*dxc.exe" ^| findstr /i x64') do set DXC=%%f
"%DXC%" -nologo -T cs_6_0 -E cs -Vn MoreSm6ComputeShader -Fh more_sm6_cs.h more_sm6.hlsl || exit /b 1
"%DXC%" -nologo -T vs_6_0 -E vs -Vn Scene6VertexShader -Fh scene6_vs.h scene.hlsl || exit /b 1
"%DXC%" -nologo -T ps_6_0 -E ps -Vn Scene6PixelShader -Fh scene6_ps.h scene.hlsl || exit /b 1
"%DXC%" -nologo -T lib_6_3 -Vn MoreDxrLibrary -Fh more_dxr.h more_dxr.hlsl || exit /b 1
